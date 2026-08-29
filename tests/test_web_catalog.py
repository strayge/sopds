"""Web adapter tests for catalog rendering, status polling, and manual import CSRF."""

import asyncio
import html
import io
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import override
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from sopds.acquisition.archive import (
    ArchiveEntryStatus,
    ArchiveLimitError,
    ArchiveManifest,
    ArchiveMember,
    ArchiveNoDownloadsError,
    ArchivePreviewEntry,
    ArchiveRequest,
    StagedArchive,
)
from sopds.acquisition.contracts import (
    AcquiredOriginal,
    AcquisitionCorruptError,
    AcquisitionNotFoundError,
    AcquisitionSourceIOError,
    AcquisitionStoreShutdownError,
    SourceRevision,
)
from sopds.catalog.contracts import (
    BookAvailability,
    BookDetail,
    BookSummary,
    CatalogFilters,
    CatalogInputError,
    CatalogPage,
    CatalogRequest,
    CatalogStatistics,
    FilterOption,
    SearchField,
)
from sopds.imports.status import ImportState, ImportStatus, ImportTrigger
from sopds.web import routes
from sopds.web.csrf import issue_csrf_token

_REVISION = SourceRevision(1, 2, 3)


class _Catalog:
    def __init__(self) -> None:
        self.requests: list[CatalogRequest] = []
        self.filter_calls = 0
        self.statistics_calls = 0
        self.statistics_failures_remaining = 0
        self.filter_failures_remaining = 0
        self.detail_requests: list[tuple[bool, bool]] = []
        self.detail_title = "A Book"
        self.detail_published_date: date | None = None
        self.detail_rating: int | None = None
        self.detail_keywords: str | None = None
        self.detail_libid: str | None = None
        self.detail_downloadable = True
        self.available_filters = CatalogFilters(
            languages=(FilterOption("en", "en"),),
            genres=(FilterOption("sf", "Science fiction"),),
            original_formats=(FilterOption("fb2", "fb2"),),
        )
        self.statistics_value = CatalogStatistics(
            total_books=20,
            hidden_books=3,
            missed_books=5,
            active_books=12,
            generation_activated_at=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
            database_size_bytes=2 * 1024 * 1024,
        )

    async def browse(self, request: CatalogRequest) -> CatalogPage:
        self.requests.append(request)
        if request.query == "invalid":
            raise CatalogInputError("Invalid catalog search")
        if request.query == "none":
            return CatalogPage((), None)
        authors = (
            (
                "Тестов,Тест,",
                " Примеров,Пример,Примерович",
                "Third,Author,",
                "Fourth,Author,",
                "Fifth,Author,",
            )
            if request.query == "many-authors"
            else (
                "Тестов,Тест,",
                " Примеров,Пример,Примерович",
            )
        )
        title = (
            "Очень длинное название книги для проверки многоязычного каталога"
            if request.query == "many-authors"
            else "A Book"
        )
        return CatalogPage(
            books=(
                BookSummary(
                    public_id="public-1",
                    title=title,
                    authors=authors,
                    series="Series",
                    series_number="1",
                    language=None if request.query == "sparse-metadata" else "en",
                    original_format="fb2",
                    size=126_000,
                    published_date=(
                        None if request.query == "sparse-metadata" else date(2024, 2, 3)
                    ),
                    availability=(
                        BookAvailability.HIDDEN
                        if request.query in {"hidden", "hidden-unavailable"}
                        else BookAvailability.MISSED
                        if request.query == "missed"
                        else BookAvailability.ACTIVE
                    ),
                    downloadable=request.query != "hidden-unavailable",
                ),
            ),
            next_cursor="next-token" if request.cursor is None else None,
        )

    async def details(
        self,
        public_id: str,
        *,
        include_missed: bool = False,
        include_hidden: bool = False,
    ) -> BookDetail | None:
        self.detail_requests.append((include_missed, include_hidden))
        if public_id != "public-1":
            return None
        availability = (
            BookAvailability.HIDDEN
            if include_hidden
            else BookAvailability.MISSED
            if include_missed
            else BookAvailability.ACTIVE
        )
        return BookDetail(
            public_id=public_id,
            title=self.detail_title,
            authors=(
                "Тестов,Тест,",
                " Примеров,Пример,Примерович",
            ),
            genres=(("sf", "Science fiction"),),
            series="Series",
            series_number="1",
            size=126_000,
            libid=self.detail_libid,
            published_date=self.detail_published_date,
            language="en",
            original_format="fb2",
            rating=self.detail_rating,
            keywords=self.detail_keywords,
            availability=availability,
            downloadable=self.detail_downloadable,
        )

    async def filters(self) -> CatalogFilters:
        self.filter_calls += 1
        if self.filter_failures_remaining:
            self.filter_failures_remaining -= 1
            raise CatalogInputError("Catalog changed while loading; retry the request")
        return self.available_filters

    async def statistics(self) -> CatalogStatistics:
        self.statistics_calls += 1
        if self.statistics_failures_remaining:
            self.statistics_failures_remaining -= 1
            raise CatalogInputError("Catalog changed while loading; retry the request")
        return self.statistics_value


class _Stream:
    def __init__(self, body: bytes = b"original") -> None:
        self.body = body
        self.closed = False
        self.iterated = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        self.iterated = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


class _Acquisition:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.stream = _Stream()

    async def acquire(self, public_id: str) -> AcquiredOriginal:
        if self.error is not None:
            raise self.error
        return AcquiredOriginal(
            filename="Книга.fb2",
            media_type="application/x-fictionbook+xml",
            content_length=len(self.stream.body),
            stream=self.stream,
            source_format="fb2",
            source_revision=_REVISION,
        )


class _Archive:
    def __init__(self) -> None:
        self.preview_requests: list[ArchiveRequest] = []
        self.download_requests: list[ArchiveRequest] = []
        self.preview_error: Exception | None = None
        self.preview_value: ArchiveManifest | None = None
        self.download_error: Exception | None = None
        self.body = b"staged archive"
        self.last_file: io.BytesIO | None = None

    async def preview(self, request: ArchiveRequest) -> ArchiveManifest:
        self.preview_requests.append(request)
        if self.preview_error is not None:
            raise self.preview_error
        if self.preview_value is not None:
            return self.preview_value
        if not request.ids:
            return ArchiveManifest(request, 7, (), (), 0)
        summary = BookSummary(
            public_id=request.ids[0],
            title="Selected Book",
            authors=("Reader,One,",),
            series=None,
            series_number=None,
            language="en",
            original_format="fb2",
            size=321,
        )
        member = ArchiveMember(
            summary.public_id,
            summary,
            "Reader One/Selected Book.fb2",
            "Reader One/Selected Book.fb2",
            collision=True,
            collision_group="reader one/selected book.fb2",
        )
        entries = (
            ArchivePreviewEntry(
                summary.public_id,
                summary,
                ArchiveEntryStatus.DOWNLOADABLE,
                collision=True,
                collision_group=member.collision_group,
            ),
            *(
                ArchivePreviewEntry(public_id, None, ArchiveEntryStatus.UNKNOWN)
                for public_id in request.ids[1:]
            ),
        )
        return ArchiveManifest(request, 7, entries, (member,), summary.size)

    async def download(self, request: ArchiveRequest) -> StagedArchive:
        self.download_requests.append(request)
        if self.download_error is not None:
            raise self.download_error
        self.last_file = io.BytesIO(self.body)
        return StagedArchive(self.last_file, len(self.body))


class _Imports:
    def __init__(self, status: ImportStatus | None = None, *, active: bool = False) -> None:
        self.status = status
        self.active = active
        self.status_calls = 0
        self.active_calls = 0
        self.accept = True
        self.started: list[bool] = []
        self.vacuumed = True
        self.vacuum_calls = 0

    async def get_status(self) -> ImportStatus | None:
        self.status_calls += 1
        return self.status

    def is_import_active(self) -> bool:
        self.active_calls += 1
        return self.active

    def start_manual_import(self, *, force: bool = False) -> bool:
        self.started.append(force)
        return self.accept

    async def vacuum_database(self) -> bool:
        self.vacuum_calls += 1
        return self.vacuumed


def _link_href(markup: str, test_id: str) -> str:
    match = re.search(rf'<a data-testid="{re.escape(test_id)}" href="([^"]+)"', markup)
    assert match is not None
    return html.unescape(match.group(1))


def _detail_href(markup: str) -> str:
    match = re.search(r'href="(/books/public-1\?[^"]+)"', markup)
    assert match is not None
    return html.unescape(match.group(1))


def _status(
    state: ImportState,
    run_id: int = 1,
    *,
    error_summary: str | None = None,
    records_read: int = 3,
    records_imported: int = 2,
    records_deleted: int = 1,
    records_rejected: int = 0,
) -> ImportStatus:
    return ImportStatus(
        run_id=run_id,
        trigger=ImportTrigger.MANUAL,
        state=state,
        started_at=datetime(2025, 1, 1, tzinfo=UTC),
        finished_at=None if state is ImportState.RUNNING else datetime(2025, 1, 2, tzinfo=UTC),
        attempted_fingerprint=None,
        records_read=records_read,
        records_imported=records_imported,
        records_deleted=records_deleted,
        records_rejected=records_rejected,
        error_summary=error_summary,
        generation_id=7,
    )


def _app(imports: _Imports | None = None) -> tuple[FastAPI, _Catalog, _Imports]:
    app = FastAPI()
    catalog = _Catalog()
    import_provider = imports or _Imports()
    app.state.catalog = catalog
    app.state.config = SimpleNamespace(
        server=SimpleNamespace(base_url="https://catalog.example/root/")
    )
    app.state.import_coordinator = import_provider
    app.state.acquisition = _Acquisition()
    app.state.archive = _Archive()
    app.state.csrf_key = b"c" * 32
    static = Path(routes.__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")
    app.include_router(routes.router)
    return app, catalog, import_provider


def _csrf_token(app: FastAPI) -> str:
    return issue_csrf_token(app.state.csrf_key)


def _download_form(app: FastAPI, ids: str, preset: str) -> dict[str, str]:
    return {"ids": ids, "preset": preset, "csrf_token": _csrf_token(app)}


def _csrf_form_suffix(app: FastAPI) -> str:
    return "&" + urlencode({"csrf_token": _csrf_token(app)})


def test_full_page_fragment_filters_pagination_and_details() -> None:
    app, catalog, _ = _app()
    with TestClient(app) as client:
        page = client.get("/?q=book&search_field=title&language=en&genre=sf&original_format=fb2")
        management = client.get("/manage")
        fragment = client.get("/catalog-fragment?q=book&search_field=author&language=en&genre=sf")
        full_next = client.get(
            "/?q=book&search_field=title&language=en&genre=sf&original_format=fb2&cursor=next-token"
        )
        detail = client.get("/books/public-1")
        missing = client.get("/books/missing")
        author_page = client.get("/", params={"author": "Тестов,Тест,"})
        series_page = client.get("/", params={"series": "Series"})

    assert page.status_code == 200
    assert "A Book" in page.text
    assert "Тестов Тест" in page.text
    assert "Примеров Пример Примерович" in page.text
    assert "Тестов,Тест" not in page.text
    assert "2024-02-03" in page.text
    assert "123 KB" in page.text
    assert (
        'href="/?author=%D0%A2%D0%B5%D1%81%D1%82%D0%BE%D0%B2%2C%D0%A2%D0%B5%D1%81%D1%82%2C"'
        in page.text
    )
    assert 'href="/?series=Series"' in page.text
    assert "Science fiction" in page.text
    assert 'href="#main-content">Skip to main content</a>' in page.text
    assert '<a href="/" aria-current="page">Catalog</a>' in page.text
    assert '<a href="/manage">Manage</a>' in page.text
    assert "/static/css/app.css" in page.text
    assert "/static/vendor/htmx/htmx-2.0.10.min.js" in page.text
    assert page.text.index('id="health"') < page.text.index('id="main-content"')
    assert (
        '<link rel="alternate" '
        'type="application/atom+xml;profile=opds-catalog;kind=navigation" '
        'href="https://catalog.example/root/opds/">'
    ) in page.text
    assert "next-token" in page.text
    assert 'action="/"' in page.text
    assert 'method="get"' in page.text
    assert 'hx-get="/catalog-fragment"' in page.text
    assert 'hx-target="#catalog-results"' in page.text
    assert 'hx-indicator="#catalog-loading"' in page.text
    assert 'hx-disabled-elt="#catalog-submit"' in page.text
    assert 'id="catalog-submit" type="submit">Search library</button>' in page.text
    assert "Catalog management" not in page.text
    assert 'id="catalog-statistics"' not in page.text
    assert 'id="operation-status"' not in page.text
    assert 'hx-post="/imports"' not in page.text
    assert 'hx-post="/imports/force"' not in page.text
    assert 'hx-post="/database/vacuum"' not in page.text
    assert "catalog-more-filters" not in page.text
    assert "More filters" not in page.text
    assert 'class="catalog-filter--genre" for="catalog-genre"' in page.text
    assert '<option value="sf" selected>Science fiction</option>' in page.text
    assert 'name="cursor"' not in page.text
    assert '<option value="title" selected>Title</option>' in page.text
    assert management.status_code == 200
    assert management.headers["cache-control"] == "no-store"
    assert "set-cookie" not in management.headers
    assert "/static/csrf.js" in management.text
    assert '<a href="/">Catalog</a>' in management.text
    assert '<a href="/manage" aria-current="page">Manage</a>' in management.text
    assert "Manage catalog" in management.text
    assert "Total books</dt><dd>20" in management.text
    assert "Hidden books</dt><dd>3" in management.text
    assert "Missed books</dt><dd>5" in management.text
    assert "Active books</dt><dd>12" in management.text
    assert "2.0 MiB" in management.text
    assert 'datetime="2025-01-02T03:04:05+00:00"' in management.text
    assert (
        'id="catalog-statistics" class="catalog-statistics" hx-get="/catalog-statistics" '
        'hx-trigger="catalogChanged from:body" hx-swap="outerHTML"' in management.text
    )
    assert 'hx-post="/imports"' in management.text
    assert 'hx-post="/imports/force"' in management.text
    assert 'hx-post="/database/vacuum"' in management.text
    assert 'hx-confirm="Force a full catalog import?"' in management.text
    assert 'hx-confirm="VACUUM the catalog database now?"' in management.text
    assert "trusted network or an authenticating reverse proxy" not in management.text
    assert "Access reminder" not in management.text
    assert "management-trust-notice" not in management.text
    assert management.text.count('hx-target="#operation-status"') == 3
    assert management.text.count('hx-headers=\'{"X-CSRF-Token":"') == 3
    assert management.text.index('id="operation-status"') > management.text.index(
        'hx-post="/database/vacuum"'
    )
    assert 'class="import-status import-status--idle"' in management.text
    assert 'role="status"' in management.text
    assert "No import has run yet" in management.text
    assert (
        'href="/?q=book&amp;search_field=title&amp;language=en&amp;genre=sf&amp;original_format=fb2&amp;cursor=next-token"'
        in page.text
    )
    assert (
        'hx-get="/catalog-fragment?q=book&amp;search_field=title&amp;language=en&amp;genre=sf&amp;original_format=fb2&amp;cursor=next-token"'
        in page.text
    )
    assert 'hx-push-url="true"' not in page.text
    assert '<a id="catalog-pagination-position" class="catalog-pagination__next"' in page.text
    assert (
        '<p id="catalog-pagination-position" class="catalog-pagination__end" '
        'tabindex="-1">End of results</p>' in full_next.text
    )
    assert "Next page" not in full_next.text
    assert 'role="status" aria-live="polite">Showing 1 book' in page.text
    assert 'class="book-tile book-tile--1" aria-hidden="true">A</div>' in page.text
    metadata = re.search(
        r'<ul class="result-metadata" aria-label="Book metadata">(.*?)</ul>',
        page.text,
        re.S,
    )
    assert metadata is not None
    assert metadata.group(1).count('class="result-metadata__line"') == 2
    assert metadata.group(1).count('class="result-metadata__separator"') == 2
    assert metadata.group(1).index("Format:") < metadata.group(1).index("Language:")
    assert metadata.group(1).index("Language:") < metadata.group(1).index("Published:")
    assert metadata.group(1).index("Published:") < metadata.group(1).index("Size:")
    assert "FB2" in metadata.group(1)
    assert "EN" in metadata.group(1)
    assert "2024-02-03" in metadata.group(1)
    assert "123 KB" in metadata.group(1)
    download_action = '<a class="result-row__download" href="/books/public-1/download">Download</a>'
    detail_action = '<a class="result-row__action" href="/books/public-1'
    assert download_action in page.text
    assert ">Open details</a>" in page.text
    assert page.text.index(download_action) < page.text.index(detail_action)
    assert fragment.status_code == 200
    assert fragment.headers["HX-Push-Url"] == (
        "/?q=book&search_field=author&language=en&genre=sf&original_format=&cursor="
    )
    assert "/catalog-fragment" not in fragment.headers["HX-Push-Url"]
    assert "<html" not in fragment.text
    assert full_next.status_code == 200
    assert (
        CatalogRequest(
            query="book",
            search_field=SearchField.TITLE,
            language="en",
            genre="sf",
            original_format="fb2",
            cursor="next-token",
            page_size=200,
        )
        in catalog.requests
    )
    assert detail.status_code == 200
    assert '<a href="/" aria-current="page">Catalog</a>' in detail.text
    assert "/static/css/app.css" in detail.text
    assert "/static/vendor/htmx/htmx-2.0.10.min.js" in detail.text
    assert "Application is healthy" in detail.text
    assert "/health-fragment" not in detail.text
    assert "Original format" in detail.text
    assert "Тестов Тест" in detail.text
    assert "Примеров Пример Примерович" in detail.text
    assert "Тестов,Тест" not in detail.text
    assert (
        'href="/?author=%D0%A2%D0%B5%D1%81%D1%82%D0%BE%D0%B2%2C%D0%A2%D0%B5%D1%81%D1%82%2C"'
        in detail.text
    )
    assert 'href="/?series=Series"' in detail.text
    assert "<dt>File size</dt>" in detail.text
    assert "<dd>123 KB</dd>" in detail.text
    assert "Back to catalog" in detail.text
    assert "availability-badge--active" not in detail.text
    assert "Download original · FB2 · 123 KB" in detail.text
    assert 'href="/books/public-1/download"' in detail.text
    assert "Published" not in detail.text
    assert "Rating" not in detail.text
    assert "Keywords" not in detail.text
    assert "Library ID" not in detail.text
    assert missing.status_code == 404
    assert author_page.status_code == 200
    assert series_page.status_code == 200
    assert CatalogRequest(author="Тестов,Тест,", page_size=200) in catalog.requests
    assert CatalogRequest(series="Series", page_size=200) in catalog.requests
    assert catalog.requests[0] == CatalogRequest(
        query="book",
        search_field=SearchField.TITLE,
        language="en",
        genre="sf",
        original_format="fb2",
        page_size=200,
    )


def test_manage_page_groups_counts_localizes_times_and_preserves_action_contracts() -> None:
    imports = _Imports(
        _status(
            ImportState.SUCCEEDED,
            records_read=702_461,
            records_imported=589_111,
            records_deleted=113_350,
            records_rejected=1_234,
        )
    )
    app, catalog, _ = _app(imports)
    catalog.statistics_value = CatalogStatistics(
        total_books=702_461,
        active_books=589_111,
        hidden_books=113_350,
        missed_books=1_234,
        generation_activated_at=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
        database_size_bytes=2 * 1024 * 1024,
    )

    with TestClient(app) as client:
        management = client.get("/manage")
        status_fragment = client.get("/imports/status")

    assert management.status_code == 200
    for label, value in (
        ("Total books", "702 461"),
        ("Active books", "589 111"),
        ("Hidden books", "113 350"),
        ("Missed books", "1 234"),
        ("Imported", "589 111"),
        ("Deleted", "113 350"),
        ("Rejected", "1 234"),
        ("Read", "702 461"),
    ):
        assert f"<dt>{label}</dt><dd>{value}</dd>" in management.text

    assert '<time class="local-datetime" datetime="2025-01-02T03:04:05+00:00">' in management.text
    assert '<time class="local-datetime" datetime="2025-01-02T00:00:00+00:00">' in management.text
    assert 'class="local-datetime" datetime="2025-01-02T00:00:00+00:00"' in status_fragment.text

    for target, label, confirmation in (
        ("/imports", "Import changes", None),
        ("/imports/force", "Force import", "Force a full catalog import?"),
        ("/database/vacuum", "Vacuum database", "VACUUM the catalog database now?"),
    ):
        button = re.search(
            rf'<button\b(?=[^>]*hx-post="{re.escape(target)}")[^>]*>'
            rf"{re.escape(label)}</button>",
            management.text,
            re.S,
        )
        assert button is not None
        assert 'hx-target="#operation-status"' in button.group(0)
        assert 'hx-headers=\'{"X-CSRF-Token":"' in button.group(0)
        assert f">{label}</button>" in button.group(0)
        if confirmation is None:
            assert "hx-confirm=" not in button.group(0)
        else:
            assert f'hx-confirm="{confirmation}"' in button.group(0)

    for removed_copy in (
        "Access reminder",
        "trusted network",
        "Counts and storage details",
        "The catalog changed while its statistics were loading",
        "Check the configured INPX source",
        "process only changes detected",
        "Reprocess the source",
        "Reclaim unused SQLite storage",
        "Current or most recently completed import activity",
    ):
        assert removed_copy not in management.text
    assert management.text.count('class="management-section__heading"') == 3
    assert 'class="management-operations" role="group"' in management.text


def test_full_page_catalog_error_uses_shared_shell() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.get("/?q=invalid")

    assert response.status_code == 400
    assert "Invalid catalog search" in response.text
    assert 'role="alert"' in response.text
    assert '<a href="/" aria-current="page">Catalog</a>' in response.text
    assert "/static/css/app.css" in response.text
    assert "/static/vendor/htmx/htmx-2.0.10.min.js" in response.text
    assert "Application is healthy" in response.text
    assert "/health-fragment" not in response.text
    assert 'href="https://catalog.example/root/opds/"' in response.text


def test_htmx_catalog_validation_error_swaps_complete_form_and_updates_history() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        htmx_response = client.get(
            "/catalog-fragment",
            params={
                "q": "invalid",
                "search_field": "title",
                "language": "en",
                "genre": "sf",
                "original_format": "fb2",
                "include_missed": "true",
                "include_hidden": "true",
            },
            headers={"HX-Request": "true"},
        )
        direct_fragment = client.get("/catalog-fragment?q=invalid")

    assert htmx_response.status_code == 200
    assert 'class="error" role="alert">Invalid catalog search</p>' in htmx_response.text
    assert htmx_response.headers["HX-Push-Url"] == (
        "/?q=invalid&search_field=title&language=en&genre=sf&original_format=fb2&cursor="
        "&include_missed=true&include_hidden=true"
    )
    form = htmx_response.text[htmx_response.text.index("<form") :]
    assert 'id="catalog-search-form"' in form
    assert 'hx-swap-oob="outerHTML"' in form
    assert 'name="q" type="search" value="invalid"' in form
    assert '<option value="title" selected>Title</option>' in form
    assert '<option value="en" selected>en</option>' in form
    assert '<option value="sf" selected>Science fiction</option>' in form
    assert '<option value="fb2" selected>fb2</option>' in form
    assert 'name="include_missed" value="true" checked' in form
    assert 'name="include_hidden" value="true" checked' in form
    assert "catalog-more-filters" not in form
    assert "More filters" not in form
    assert "Include missing" in form
    assert "Include hidden" in form
    assert direct_fragment.status_code == 400
    assert "HX-Push-Url" not in direct_fragment.headers
    assert 'id="catalog-search-form"' not in direct_fragment.text


def test_direct_catalog_fragments_do_not_load_filters_during_generation_races() -> None:
    app, catalog, _ = _app()
    catalog.filter_failures_remaining = 1
    with TestClient(app) as client:
        success = client.get("/catalog-fragment?q=book")
        invalid = client.get("/catalog-fragment?q=invalid")
        filter_race = client.get("/")
        recovered = client.get("/")

    assert success.status_code == 200
    assert "A Book" in success.text
    assert invalid.status_code == 400
    assert 'role="alert">Invalid catalog search' in invalid.text
    assert 'id="catalog-search-form"' not in success.text
    assert 'id="catalog-search-form"' not in invalid.text
    assert filter_race.status_code == 400
    assert "Catalog changed while loading; retry the request" in filter_race.text
    assert recovered.status_code == 200
    assert catalog.filter_calls == 2


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("book", "Catalog changed while loading; retry the request"),
        ("invalid", "Invalid catalog search"),
    ],
)
def test_htmx_filter_generation_races_return_alert_without_incomplete_form(
    query: str,
    message: str,
) -> None:
    app, catalog, _ = _app()
    catalog.filter_failures_remaining = 1
    with TestClient(app) as client:
        response = client.get(
            "/catalog-fragment",
            params={
                "q": query,
                "search_field": "title",
                "language": "en",
                "genre": "sf",
                "original_format": "fb2",
                "cursor": "current-page",
            },
            headers={"HX-Request": "true"},
        )
        recovered = client.get("/")

    assert response.status_code == 200
    assert f'role="alert">{message}' in response.text
    assert 'id="catalog-search-form"' not in response.text
    assert 'hx-swap-oob="outerHTML"' not in response.text
    assert response.headers["HX-Push-Url"] == (
        f"/?q={query}&search_field=title&language=en&genre=sf&original_format=fb2"
        "&cursor=current-page"
    )
    assert recovered.status_code == 200
    assert catalog.filter_calls == 2


def test_htmx_catalog_response_replaces_complete_current_form_out_of_band() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        initial = client.get("/")
        active = client.get(
            "/catalog-fragment",
            params={
                "q": "book",
                "search_field": "title",
                "language": "en",
                "genre": "sf",
                "original_format": "fb2",
                "author": "Тестов,Тест,",
                "series": "Series",
                "include_missed": "true",
                "include_hidden": "true",
                "cursor": "next-token",
            },
            headers={"HX-Request": "true"},
        )

    assert initial.status_code == 200
    assert 'id="catalog-search-form"' in initial.text
    assert 'hx-swap-oob="outerHTML"' not in initial.text
    assert active.status_code == 200
    assert active.headers["HX-Push-Url"].endswith(
        "&cursor=next-token&author=%D0%A2%D0%B5%D1%81%D1%82%D0%BE%D0%B2%2C%D0%A2%D0%B5%D1%81%D1%82%2C"
        "&series=Series&include_missed=true&include_hidden=true"
    )
    form = active.text[active.text.index("<form") : active.text.index("</form>")]
    assert 'id="catalog-search-form"' in form
    assert 'hx-swap-oob="outerHTML"' in form
    assert 'name="q" type="search" value="book"' in form
    assert '<option value="title" selected>Title</option>' in form
    assert '<option value="en" selected>en</option>' in form
    assert '<option value="sf" selected>Science fiction</option>' in form
    assert '<option value="fb2" selected>fb2</option>' in form
    assert 'type="hidden" name="author" value="Тестов,Тест,"' in form
    assert 'type="hidden" name="series" value="Series"' in form
    assert "Author: Тестов Тест" in form
    assert "Series: Series" in form
    assert 'name="include_missed" value="true" checked' in form
    assert 'name="include_hidden" value="true" checked' in form
    assert "catalog-more-filters" not in form
    assert "Include missing" in form
    assert "Include hidden" in form
    assert (
        'id="catalog-clear-action" class="catalog-clear" href="/" '
        'aria-label="Clear search and filters">Clear all</a>' in form
    )
    assert 'name="cursor"' not in form


def test_missing_selected_filter_options_are_retained_in_full_and_oob_forms() -> None:
    app, catalog, _ = _app()
    catalog.available_filters = CatalogFilters(
        languages=(FilterOption("en", "English"), FilterOption("fr", "French")),
        genres=(FilterOption("sf", "Science fiction"), FilterOption("fantasy", "Fantasy")),
        original_formats=(FilterOption("fb2", "FictionBook"), FilterOption("epub", "EPUB")),
    )
    params = {
        "q": "book",
        "language": "de",
        "genre": "historical",
        "original_format": "mobi",
    }
    with TestClient(app) as client:
        full_page = client.get("/", params=params)
        htmx_response = client.get(
            "/catalog-fragment",
            params=params,
            headers={"HX-Request": "true"},
        )

    assert full_page.status_code == 200
    assert full_page.text.index('<option value="fr">French</option>') < full_page.text.index(
        '<option value="de" selected>de</option>'
    )
    assert full_page.text.index('<option value="fantasy">Fantasy</option>') < full_page.text.index(
        '<option value="historical" selected>historical</option>'
    )
    assert full_page.text.index('<option value="epub">EPUB</option>') < full_page.text.index(
        '<option value="mobi" selected>mobi</option>'
    )
    assert htmx_response.status_code == 200
    form = htmx_response.text[htmx_response.text.index("<form") :]
    assert 'hx-swap-oob="outerHTML"' in form
    assert '<option value="de" selected>de</option>' in form
    assert '<option value="historical" selected>historical</option>' in form
    assert '<option value="mobi" selected>mobi</option>' in form


def test_optional_missed_and_hidden_search_scopes_are_preserved() -> None:
    app, catalog, _ = _app()
    with TestClient(app) as client:
        page = client.get("/?q=hidden&include_missed=true&include_hidden=true")
        fragment = client.get("/catalog-fragment?q=missed&include_missed=true&include_hidden=true")

    assert page.status_code == 200
    assert 'name="include_missed" value="true" checked' in page.text
    assert 'name="include_hidden" value="true" checked' in page.text
    assert "catalog-more-filters" not in page.text
    assert "Include missing" in page.text
    assert "Include hidden" in page.text
    assert 'class="availability-badge availability-badge--hidden">Hidden</span>' in page.text
    detail_href = _detail_href(page.text)
    detail_query = parse_qs(urlsplit(detail_href).query)
    assert detail_query["include_missed"] == ["true"]
    assert detail_query["include_hidden"] == ["true"]
    assert detail_query["return_to"] == [
        "/?q=hidden&search_field=all&language=&genre=&original_format=&cursor="
        "&include_missed=true&include_hidden=true"
    ]
    assert fragment.status_code == 200
    assert 'class="availability-badge availability-badge--missed">Missed</span>' in fragment.text
    assert 'class="result-row__download"' not in fragment.text
    assert ">Open details</a>" in fragment.text
    assert fragment.headers["HX-Push-Url"].endswith("&include_missed=true&include_hidden=true")
    assert (
        CatalogRequest(query="hidden", include_missed=True, include_hidden=True, page_size=200)
        in catalog.requests
    )
    assert (
        CatalogRequest(query="missed", include_missed=True, include_hidden=True, page_size=200)
        in catalog.requests
    )


def test_unavailable_hidden_book_has_no_catalog_or_detail_download_action() -> None:
    app, catalog, _ = _app()
    catalog.detail_downloadable = False
    with TestClient(app) as client:
        results = client.get("/?q=hidden-unavailable&include_hidden=true")
        detail = client.get("/books/public-1?include_hidden=true")

    assert results.status_code == 200
    assert 'class="availability-badge availability-badge--hidden">Hidden</span>' in results.text
    assert 'class="result-row__download"' not in results.text
    assert detail.status_code == 200
    assert "Original file unavailable" in detail.text
    assert 'href="/books/public-1/download"' not in detail.text


def test_result_detail_link_preserves_exact_catalog_context() -> None:
    app, catalog, _ = _app()
    params = {
        "q": "книга",
        "search_field": "title",
        "language": "ru",
        "genre": "sf",
        "original_format": "fb2",
        "cursor": "opaque/token",
        "author": "Тестов,Тест,",
        "series": "Series & More",
        "include_missed": "true",
        "include_hidden": "true",
    }
    with TestClient(app) as client:
        results = client.get("/", params=params)
        detail_href = _detail_href(results.text)
        detail = client.get(detail_href)

    expected_return = (
        "/?q=%D0%BA%D0%BD%D0%B8%D0%B3%D0%B0&search_field=title&language=ru&genre=sf"
        "&original_format=fb2&cursor=opaque%2Ftoken"
        "&author=%D0%A2%D0%B5%D1%81%D1%82%D0%BE%D0%B2%2C%D0%A2%D0%B5%D1%81%D1%82%2C"
        "&series=Series+%26+More&include_missed=true&include_hidden=true"
    )
    query = parse_qs(urlsplit(detail_href).query)
    assert query == {
        "return_to": [expected_return],
        "include_missed": ["true"],
        "include_hidden": ["true"],
    }
    assert detail.status_code == 200
    assert _link_href(detail.text, "detail-back-link") == expected_return
    assert "Back to results" in detail.text
    assert catalog.detail_requests[-1] == (True, True)
    assert (
        'href="/?author=%D0%A2%D0%B5%D1%81%D1%82%D0%BE%D0%B2%2C%D0%A2%D0%B5%D1%81%D1%82%2C'
        '&amp;include_missed=true&amp;include_hidden=true"' in detail.text
    )
    assert 'href="/?series=Series&amp;include_missed=true&amp;include_hidden=true"' in detail.text


@pytest.mark.parametrize(
    "return_to",
    [
        "https://example.invalid/?q=book",
        "//example.invalid/?q=book",
        "///?q=book",
        "",
        "?q=book",
        "%2F%3Fq%3Dbook",
        "/?q=book#section",
        "/?q=book\\catalog",
        "/?q=book\nnext",
        "/manage?q=book",
        "relative",
        "/?q=%ZZ",
        "//[invalid",
    ],
)
def test_book_detail_rejects_unsafe_return_urls(return_to: str) -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.get("/books/public-1", params={"return_to": return_to})

    assert response.status_code == 200
    assert _link_href(response.text, "detail-back-link") == "/"
    assert "Back to catalog" in response.text


def test_book_detail_accepts_catalog_root_without_a_query() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.get("/books/public-1", params={"return_to": "/"})

    assert response.status_code == 200
    assert _link_href(response.text, "detail-back-link") == "/"
    assert "Back to results" in response.text


def test_book_detail_renders_present_metadata_and_availability_actions() -> None:
    app, catalog, _ = _app()
    catalog.detail_title = "Очень длинное многоязычное название книги"
    catalog.detail_published_date = date(2024, 2, 3)
    catalog.detail_rating = 5
    catalog.detail_keywords = "one, два"
    catalog.detail_libid = "library-7"
    with TestClient(app) as client:
        active = client.get("/books/public-1")
        hidden = client.get("/books/public-1?include_hidden=true")
        missed = client.get("/books/public-1?include_missed=true")

    assert active.status_code == 200
    assert "Очень длинное многоязычное название книги" in active.text
    assert (
        'class="book-tile book-detail__tile book-tile--1" aria-hidden="true">\u041e</div>'
        in active.text
    )
    assert "<dt>Published</dt><dd>2024-02-03</dd>" in active.text
    assert "<dt>Rating</dt><dd>5</dd>" in active.text
    assert "<dt>Library ID</dt><dd>library-7</dd>" in active.text
    assert 'class="tag" href="/?genre=sf">Science fiction</a>' in active.text
    assert 'class="tag tag--text">one</span>' in active.text
    assert 'class="tag tag--text">два</span>' in active.text
    assert "Download original · FB2 · 123 KB" in active.text
    assert 'availability-badge--hidden">Hidden</span>' in hidden.text
    assert "Download original · FB2 · 123 KB" in hidden.text
    assert 'href="/?series=Series&amp;include_hidden=true"' in hidden.text
    assert 'availability-badge--missed">Missed</span>' in missed.text
    assert "Original file unavailable" in missed.text
    assert "Download original" not in missed.text
    assert 'href="/books/public-1/download"' not in missed.text


def test_active_scopes_are_visible_preserved_and_removable_without_cursor() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        page = client.get(
            "/",
            params={
                "q": "book",
                "author": "Тестов,Тест,",
                "series": "Series",
                "cursor": "old-page",
            },
        )

    assert page.status_code == 200
    assert 'type="hidden" name="author" value="Тестов,Тест,"' in page.text
    assert 'type="hidden" name="series" value="Series"' in page.text
    assert "Author: Тестов Тест" in page.text
    assert "Series: Series" in page.text
    assert 'aria-label="Remove author scope Тестов Тест"' in page.text
    assert 'aria-label="Remove series scope Series"' in page.text
    assert (
        'href="/?q=book&amp;search_field=all&amp;language=&amp;genre=&amp;original_format=&amp;series=Series"'
        in page.text
    )
    assert (
        'href="/?q=book&amp;search_field=all&amp;language=&amp;genre=&amp;original_format=&amp;author=%D0%A2%D0%B5%D1%81%D1%82%D0%BE%D0%B2%2C%D0%A2%D0%B5%D1%81%D1%82%2C"'
        in page.text
    )
    assert 'name="cursor"' not in page.text
    assert (
        'class="catalog-clear" href="/" aria-label="Clear search and filters">Clear all</a>'
        in page.text
    )


def test_long_author_lists_use_native_overflow_disclosure() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        page = client.get("/?q=many-authors")

    assert page.status_code == 200
    assert "Очень длинное название книги для проверки многоязычного каталога" in page.text
    assert 'aria-hidden="true">\u041e</div>' in page.text
    assert "Тестов Тест" in page.text
    assert "Примеров Пример Примерович" in page.text
    assert "Third Author" in page.text
    assert '<details class="author-overflow">' in page.text
    assert "<summary>+2 more</summary>" in page.text
    assert page.text.count('class="result-row__author-token"') == 5
    assert re.search(
        r'class="result-row__author-token"><a [^>]+>Тестов Тест</a>'
        r'<span aria-hidden="true">,</span></span>',
        page.text,
    )
    assert re.search(
        r'class="result-row__author-token"><a [^>]+>Fourth Author</a>'
        r'<span aria-hidden="true">,</span></span>',
        page.text,
    )
    assert "Fourth Author" in page.text
    assert "Fifth Author" in page.text


def test_catalog_metadata_groups_two_lines_without_dangling_separators() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        page = client.get("/?q=sparse-metadata")

    assert page.status_code == 200
    metadata = re.search(
        r'<ul class="result-metadata" aria-label="Book metadata">(.*?)</ul>',
        page.text,
        re.S,
    )
    assert metadata is not None
    assert metadata.group(1).count('class="result-metadata__line"') == 2
    assert "Format:" in metadata.group(1)
    assert "Size:" in metadata.group(1)
    assert "Language:" not in metadata.group(1)
    assert "Published:" not in metadata.group(1)
    assert "result-metadata__separator" not in metadata.group(1)


def test_utility_workspace_structure_keeps_catalog_and_management_separate() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        catalog = client.get("/?q=book")
        management = client.get("/manage")

    assert catalog.status_code == 200
    assert catalog.text.index('<aside class="app-sidebar">') < catalog.text.index(
        '<div class="workspace">'
    )
    assert catalog.text.index('<header class="workspace-header">') < catalog.text.index(
        '<main id="main-content"'
    )
    assert ">Search catalog</h1>" in catalog.text
    assert "catalog-introduction" not in catalog.text
    assert 'id="catalog-search-form"\n  class="catalog-search"' in catalog.text
    assert 'class="catalog-filter-toolbar" role="group" aria-label="Search options"' in catalog.text
    assert 'class="catalog-filter--genre" for="catalog-genre"' in catalog.text
    assert 'class="catalog-availability-filters" aria-label="Availability options"' in catalog.text
    assert "catalog-more-filters" not in catalog.text
    toolbar_start = catalog.text.index('<div class="catalog-filter-toolbar"')
    form_end = catalog.text.index("</form>", toolbar_start)
    assert toolbar_start < catalog.text.index('id="catalog-loading"') < form_end
    assert toolbar_start < catalog.text.index('id="catalog-clear-action"') < form_end
    assert "catalog-search__footer" not in catalog.text
    assert re.search(
        r'class="result-row__body">.*?</div>\s*<ul class="result-metadata"',
        catalog.text,
        re.S,
    )
    assert ">Open details</a>" in catalog.text
    assert 'id="catalog-statistics"' not in catalog.text

    assert management.status_code == 200
    assert ">Manage catalog</h1>" in management.text
    assert 'id="catalog-statistics"' in management.text
    assert "management-introduction" not in management.text


def test_inline_catalog_actions_are_compact_with_touch_safe_pointer_overrides() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        stylesheet = client.get("/static/css/app.css")

    assert stylesheet.status_code == 200
    assert re.search(r"\.scope-chip a \{[^}]*min-height: 2\.75rem;", stylesheet.text, re.S)
    assert re.search(
        r"\.author-overflow summary \{[^}]*min-height: 1rem;",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.result-row__action,\s*\.result-row__download \{[^}]*min-height: 2\.125rem;",
        stylesheet.text,
        re.S,
    )
    coarse_rules = re.search(
        r"@media \(pointer: coarse\) \{(.*?)\n\}",
        stylesheet.text,
        re.S,
    )
    assert coarse_rules is not None
    for selector in (
        ".catalog-quick-filters label",
        ".catalog-availability-filters .checkbox",
        ".catalog-clear",
        ".author-overflow summary",
        ".result-row__action",
        ".result-row__download",
    ):
        assert selector in coarse_rules.group(1)
    assert "min-height: 2.75rem;" in coarse_rules.group(1)


def test_narrow_navigation_uses_two_touch_safe_rows_without_count_overflow() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        stylesheet = client.get("/static/css/app.css")

    assert stylesheet.status_code == 200
    narrow_rules = stylesheet.text.split("@media (max-width: 34rem) {", 1)[1].split(
        "@media (pointer: coarse)", 1
    )[0]
    assert re.search(
        r"\.site-navigation \{[^}]*width: 100%;[^}]*flex: 1 0 100%;[^}]*"
        r"grid-template-columns: repeat\(2, minmax\(0, 1fr\)\);",
        narrow_rules,
        re.S,
    )
    assert re.search(r"\.site-navigation a \{[^}]*min-width: 0;", narrow_rules, re.S)
    assert re.search(
        r"\.site-navigation \[data-selection-count\] \{[^}]*flex: 0 0 auto;"
        r"[^}]*margin-left: var\(--space-1\);",
        narrow_rules,
        re.S,
    )
    assert re.search(r"\.site-navigation a \{[^}]*min-height: 2\.75rem;", stylesheet.text, re.S)


def test_selected_page_preview_and_download_use_strict_matching_requests() -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive
    payload = {"ids": ["public-1", "missing", "public-1"], "preset": "nested"}

    with TestClient(app) as client:
        page = client.get("/selected")
        preview = client.post("/selected/preview", json=payload)
        download = client.post(
            "/selected/download",
            data=_download_form(app, json.dumps(payload["ids"]), str(payload["preset"])),
        )

    assert page.status_code == 200
    assert '<a href="/selected" aria-current="page">Selected <span' in page.text
    assert "data-selection-count hidden>0</span>" in page.text
    assert "/static/selection.js" in page.text
    assert 'action="/selected/download"' in page.text
    assert 'method="post"' in page.text
    assert 'name="ids" value="[]" data-selected-ids' in page.text
    assert re.search(r'name="csrf_token" value="[A-Za-z0-9_-]+"', page.text)
    assert page.headers["cache-control"] == "no-store"
    assert "set-cookie" not in page.headers
    assert "data-selected-preview-target" in page.text
    assert "data-selection-clear" in page.text
    assert "data-selected-request-status" in page.text
    assert "data-selected-download disabled" in page.text
    assert "public-1" not in page.text
    assert preview.status_code == 200
    assert 'data-selected-count="2"' in preview.text
    assert 'data-downloadable-count="1"' in preview.text
    assert 'data-total-size="321"' in preview.text
    assert 'data-catalog-generation="7"' in preview.text
    assert 'data-status="downloadable"' in preview.text
    assert 'data-status="unknown"' in preview.text
    assert 'data-collision="true"' in preview.text
    assert "Selected Book" in preview.text
    assert "Archive name conflicts; ZIP names will be made unique." in preview.text
    assert preview.text.count("data-selection-checkbox") == 2
    assert "Include Selected Book in archive" in preview.text
    assert "Include unknown selection missing in archive" in preview.text
    assert preview.text.count("data-selection-remove") == 2
    assert 'aria-label="Remove Selected Book"' in preview.text
    assert 'aria-label="Remove unknown selection missing"' in preview.text
    assert 'data-selected-summary tabindex="-1"' in preview.text
    assert "Reader One/Selected Book.fb2" not in preview.text
    assert 'href="/books/public-1?return_to=%2Fselected"' in preview.text
    assert download.status_code == 200
    assert download.content == archive.body
    assert download.headers["content-type"] == "application/zip"
    assert download.headers["content-length"] == str(len(archive.body))
    assert download.headers["x-content-type-options"] == "nosniff"
    assert 'filename="selected-books.zip"' in download.headers["content-disposition"]
    assert "filename*=UTF-8''selected-books.zip" in download.headers["content-disposition"]
    assert archive.last_file is not None and archive.last_file.closed
    assert archive.preview_requests == [ArchiveRequest(["public-1", "missing"], "nested")]
    assert archive.download_requests == [ArchiveRequest(["public-1", "missing"], "nested")]
    assert archive.preview_requests[0] is not archive.download_requests[0]


def test_catalog_selection_hooks_only_render_for_downloadable_non_missed_books() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        available = client.get("/?q=book")
        missed = client.get("/?q=missed&include_missed=true")
        unavailable = client.get("/?q=hidden-unavailable&include_hidden=true")
        management = client.get("/manage")

    assert 'data-selection-checkbox data-public-id="public-1"' in available.text
    assert "data-selection-control hidden" in available.text
    assert "availability-badge--active" not in available.text
    assert "data-selection-checkbox" not in missed.text
    assert "data-selection-checkbox" not in unavailable.text
    for response in (available, missed, unavailable, management):
        assert "<span data-selection-count hidden>0</span>" in response.text
        assert (
            '<script defer src="http://testserver/static/selection.js"></script>' in response.text
        )


def test_selected_preview_reuses_rows_and_marks_all_excluded_states_without_paths() -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive
    hidden = BookSummary(
        public_id="hidden-1",
        title="Hidden Book",
        authors=("Writer,Hidden,",),
        series="Shelf",
        series_number="2",
        language="en",
        original_format="epub",
        size=456,
        availability=BookAvailability.HIDDEN,
    )
    missed = BookSummary(
        public_id="missed-1",
        title="Missed Book",
        authors=("Writer,Missed,",),
        series=None,
        series_number=None,
        language=None,
        original_format="fb2",
        size=789,
        availability=BookAvailability.MISSED,
        downloadable=False,
    )
    member = ArchiveMember(
        hidden.public_id,
        hidden,
        "Writer Hidden/Hidden Book.epub",
        "Writer Hidden/Hidden Book.epub",
        collision=True,
        collision_group="private/path/key",
    )
    request = ArchiveRequest([hidden.public_id, missed.public_id, "unknown-1"], "nested")
    archive.preview_value = ArchiveManifest(
        request,
        7,
        (
            ArchivePreviewEntry(
                hidden.public_id,
                hidden,
                ArchiveEntryStatus.DOWNLOADABLE,
                collision=True,
                collision_group=member.collision_group,
            ),
            ArchivePreviewEntry(missed.public_id, missed, ArchiveEntryStatus.UNAVAILABLE),
            ArchivePreviewEntry("unknown-1", None, ArchiveEntryStatus.UNKNOWN),
        ),
        (member,),
        hidden.size,
    )

    with TestClient(app) as client:
        preview = client.post(
            "/selected/preview",
            json={"ids": list(request.ids), "preset": request.preset.value},
        )

    assert preview.status_code == 200
    assert preview.text.count('class="book-tile ') == 3
    assert preview.text.count('aria-label="Book metadata"') == 2
    assert preview.text.count("data-selection-checkbox") == 3
    assert "Include Hidden Book in archive" in preview.text
    assert "Include Missed Book in archive" in preview.text
    assert "Include unknown selection unknown-1 in archive" in preview.text
    assert preview.text.count("data-selection-remove") == 3
    assert 'data-status="downloadable" data-collision="true"' in preview.text
    assert 'data-status="unavailable" data-collision="false"' in preview.text
    assert 'data-status="unknown"' in preview.text
    assert "unavailable book is excluded" in preview.text
    assert "unknown selection is excluded" in preview.text
    assert "Archive name collisions affect 1 book" in preview.text
    assert "Archive name conflicts; ZIP names will be made unique." in preview.text
    assert 'aria-label="Remove Hidden Book"' in preview.text
    assert 'aria-label="Remove Missed Book"' in preview.text
    assert 'aria-label="Remove unknown selection unknown-1"' in preview.text
    assert "Writer Hidden/Hidden Book.epub" not in preview.text
    assert "private/path/key" not in preview.text
    assert 'href="/books/hidden-1?return_to=%2Fselected&amp;include_hidden=true"' in preview.text
    assert 'href="/books/missed-1?return_to=%2Fselected&amp;include_missed=true"' in preview.text


def test_selected_preview_empty_state_provides_focus_fallback() -> None:
    app, _, _ = _app()

    with TestClient(app) as client:
        preview = client.post("/selected/preview", json={"ids": [], "preset": "nested"})

    assert preview.status_code == 200
    assert 'data-selected-summary tabindex="-1"' in preview.text
    assert 'data-selected-empty tabindex="-1"' in preview.text


def test_selection_static_asset_has_browser_local_and_normal_form_contracts() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        script = client.get("/static/selection.js")
        csrf_script = client.get("/static/csrf.js")
        page = client.get("/selected")

    assert script.status_code == csrf_script.status_code == 200
    for contract in (
        '"sopds.selected-books.v1"',
        "JSON.parse",
        "JSON.stringify",
        "new Set",
        "MAX_SELECTED = 10_000",
        "value.length <= 64",
        "localStorage.getItem",
        "localStorage.setItem",
        '"DOMContentLoaded"',
        '"htmx:afterSwap"',
        '"storage"',
        "AbortController",
        "previewGeneration",
        "requestGeneration !== previewGeneration",
        "pendingPreviewFocus",
        "saveSelection([], {publicId: null}, true)",
        "resetPreviewState(page)",
        "restorePreviewFocus(target, requestIds)",
        'target.querySelector("[data-selected-preview-error]")',
        "showPreviewError(target)",
        "mergeSelectedPreview(target, incomingContent)",
        "showPreservedPreviewError(target, incomingContent)",
        "createSelectedEmptyState()",
        "const includedIds = new Set(selectedIds)",
        "hasExcludedDisplayedEntries()",
        "refreshSelectedPreview({preserveEntries})",
        'response.ok && !incomingContent.hasAttribute("data-selected-preview-error")',
        "button.dataset.publicId === preferredId",
        'window.fetch("/selected/preview"',
        '"Content-Type": "application/json"',
        "response.text()",
    ):
        assert contract in script.text
    loading_markup = "target.innerHTML = '<p class=\"selected-loading\">Loading selection…</p>';"
    fetch_start = 'const response = await window.fetch("/selected/preview"'
    assert "if (!keepEntries) {" in script.text
    assert loading_markup in script.text
    assert script.text.index(loading_markup) < script.text.index(fetch_start)
    storage_handler = re.search(
        r"function handleStorage\(event\) \{(.*?)\n  \}\n\n  function initialize",
        script.text,
        re.S,
    )
    assert storage_handler is not None
    assert "event.key !== STORAGE_KEY && event.key !== null" in storage_handler.group(1)
    assert "selectedIds = readSelection();" in storage_handler.group(1)
    assert "event.newValue" not in storage_handler.group(1)
    assert "count.textContent = String(selectedIds.length);" in script.text
    assert script.text.count("restorePreviewFocus(target, requestIds);") == 3
    refresh_body = script.text.split("async function refreshSelectedPreview(", 1)[1].split(
        "function handleChange", 1
    )[0]
    assert refresh_body.index("requestGeneration !== previewGeneration") < refresh_body.index(
        "target.innerHTML = markup;"
    )
    catch_body = refresh_body.split("} catch (error)", 1)[1]
    assert (
        catch_body.index("requestGeneration !== previewGeneration")
        < catch_body.index("showPreviewError(target);")
        < catch_body.index("restorePreviewFocus(target, requestIds);")
    )
    assert 'data-selected-preview-error role="alert" tabindex="-1"' in script.text
    assert "querySelector(`[data-public-id=" not in script.text
    assert "Blob" not in script.text
    assert 'document.addEventListener("htmx:responseError"' in csrf_script.text
    assert "xhr.status !== 403" in csrf_script.text
    assert 'xhr.getResponseHeader("X-SOPDS-CSRF-Expired")' in csrf_script.text
    assert "target.innerHTML = xhr.responseText" in csrf_script.text
    assert 'method="post" action="/selected/download"' in page.text
    assert 'type="hidden" name="ids"' in page.text
    assert 'type="hidden" name="csrf_token"' in page.text
    assert '<option value="nested" selected>' in page.text
    assert '<option value="flatten">' in page.text
    assert '<option value="list">' in page.text


@pytest.mark.parametrize(
    ("body", "status_code"),
    [
        (b'[{"ids": [], "preset": "nested"}]', 422),
        (b'{"ids": [], "preset": "nested", "extra": true}', 422),
        (b'{"ids": []}', 422),
        (b'{"ids": [], "ids": [], "preset": "nested"}', 400),
        (b'{"ids": [}', 400),
        (b"\xff", 400),
    ],
)
def test_selected_preview_rejects_non_object_extra_missing_duplicate_and_malformed_json(
    body: bytes,
    status_code: int,
) -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.post(
            "/selected/preview",
            content=body,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == status_code
    assert (
        "Invalid archive request" in response.text
        or "Invalid selected-books request" in response.text
    )
    assert len(response.content) < 1_000
    assert app.state.archive.preview_requests == []


@pytest.mark.parametrize(
    "body",
    [
        "ids=%5B%22public-1%22%5D&preset=nested&extra=x",
        "ids=%5B%22public-1%22%5D&ids=%5B%5D&preset=nested",
        "ids=%5B%22public-1%22%5D&preset=nested&csrf_token=duplicate",
        "ids=%5B%22public-1%22%5D",
        "ids=%ZZ&preset=nested",
        "ids=not-json&preset=nested",
    ],
)
def test_selected_download_rejects_extra_duplicate_missing_and_malformed_form(
    body: str,
) -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.post(
            "/selected/download",
            content=body + _csrf_form_suffix(app),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert response.status_code == 400
    assert "Invalid selected-books request" in response.text
    assert len(response.content) < 3_200
    assert app.state.archive.download_requests == []


def test_selected_download_token_supports_retries_and_changed_selection() -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive

    with TestClient(app) as client:
        token = _csrf_token(app)
        first = client.post(
            "/selected/download",
            data={"ids": '["public-1"]', "preset": "nested", "csrf_token": token},
            headers={"Origin": "https://unrelated.example", "Sec-Fetch-Site": "cross-site"},
        )
        retry = client.post(
            "/selected/download",
            data={"ids": '["public-2"]', "preset": "nested", "csrf_token": token},
        )

    assert first.status_code == retry.status_code == 200
    assert archive.download_requests == [
        ArchiveRequest(["public-1"], "nested"),
        ArchiveRequest(["public-2"], "nested"),
    ]
    assert archive.last_file is not None and archive.last_file.closed


@pytest.mark.parametrize("case", [None, "", "wrong", "expired", "other-instance"])
def test_selected_download_rejects_invalid_token_before_parsing_or_building(
    case: str | None,
) -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive

    with TestClient(app) as client:
        supplied = case
        if case == "expired":
            supplied = issue_csrf_token(app.state.csrf_key, now=0)
        elif case == "other-instance":
            supplied = issue_csrf_token(b"d" * 32)
        data = {"ids": '["public-1"]', "preset": "nested"}
        if supplied is not None:
            data["csrf_token"] = supplied
        response = client.post("/selected/download", data=data)

    assert response.status_code == 403
    assert routes._CSRF_ERROR_MESSAGE in response.text
    assert len(response.content) < 3_200
    assert archive.download_requests == []


def test_selected_json_and_form_apply_the_same_logical_validation() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        preview = client.post(
            "/selected/preview",
            json={"ids": ["public-1"], "preset": "unknown"},
        )
        download = client.post(
            "/selected/download",
            data=_download_form(app, '["public-1"]', "unknown"),
        )
        wrong_preview_type = client.post(
            "/selected/preview",
            content=b'{"ids": [], "preset": "nested"}',
            headers={"Content-Type": "text/plain"},
        )
        wrong_download_type = client.post(
            "/selected/download",
            content=b"ids=%5B%5D&preset=nested",
            headers={"Content-Type": "text/plain"},
        )

    assert preview.status_code == download.status_code == 422
    assert "Invalid archive preset" in preview.text
    assert "Invalid archive preset" in download.text
    assert wrong_preview_type.status_code == wrong_download_type.status_code == 400


def test_selected_routes_stop_oversized_bodies_before_decoding() -> None:
    app, _, _ = _app()
    oversized = b"x" * (8_388_608 + 1)
    with TestClient(app) as client:
        preview = client.post(
            "/selected/preview",
            content=oversized,
            headers={"Content-Type": "application/json"},
        )
        download = client.post(
            "/selected/download",
            content=oversized,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    assert preview.status_code == download.status_code == 413
    assert "Selected-books request is too large" in preview.text
    assert "Selected-books request is too large" in download.text
    assert app.state.archive.preview_requests == []
    assert app.state.archive.download_requests == []


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ArchiveLimitError("Too many selected books"), 413),
        (ArchiveNoDownloadsError("No selected books are available for download"), 422),
        (CatalogInputError("catalog detail that must not be reflected"), 422),
        (AcquisitionStoreShutdownError(), 503),
        (AcquisitionSourceIOError("/private/source.zip"), 500),
        (RuntimeError("/private/unexpected.zip"), 500),
    ],
)
def test_selected_download_status_mappings_are_bounded_and_path_free(
    error: Exception,
    status_code: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive
    archive.download_error = error
    with TestClient(app) as client:
        response = client.post(
            "/selected/download",
            data=_download_form(app, '["secret-public-id"]', "nested"),
        )

    assert response.status_code == status_code
    assert len(response.content) < 3_200
    assert "/private/" not in response.text
    assert "secret-public-id" not in response.text
    assert "/private/" not in caplog.text
    assert "secret-public-id" not in caplog.text


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ArchiveLimitError("Selected books exceed the source-size limit"), 413),
        (CatalogInputError("catalog detail that must not be reflected"), 422),
        (RuntimeError("/private/catalog.sqlite"), 500),
    ],
)
def test_selected_preview_status_mappings_are_bounded_and_path_free(
    error: Exception,
    status_code: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive
    archive.preview_error = error
    with TestClient(app) as client:
        response = client.post(
            "/selected/preview",
            json={"ids": ["secret-public-id"], "preset": "nested"},
        )

    assert response.status_code == status_code
    assert len(response.content) < 1_000
    assert "data-selected-preview-error" in response.text
    assert 'tabindex="-1"' in response.text
    assert "/private/" not in response.text
    assert "secret-public-id" not in response.text
    assert "/private/" not in caplog.text
    assert "secret-public-id" not in caplog.text


def _selected_download_scope() -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "https",
        "path": "/selected/download",
        "raw_path": b"/selected/download",
        "query_string": b"",
        "headers": [
            (b"host", b"catalog.example"),
            (b"content-type", b"application/x-www-form-urlencoded"),
            (b"origin", b"https://catalog.example"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("catalog.example", 443),
    }


def _selected_download_body(app: FastAPI) -> bytes:
    return urlencode(_download_form(app, '["public-1"]', "nested")).encode()


async def _run_synthetic_selected_download(app: FastAPI, receive: Receive, send: Send) -> None:
    await app(_selected_download_scope(), receive, send)


async def test_selected_download_disconnect_cancels_and_drains_blocked_build() -> None:
    app, _, _ = _app()
    build_started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()

    class BlockingArchive(_Archive):
        @override
        async def download(self, request: ArchiveRequest) -> StagedArchive:
            self.download_requests.append(request)
            build_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cleaned.set()
            raise AssertionError("cancelled build resumed")

    app.state.archive = BlockingArchive()
    disconnect = asyncio.Event()
    receive_calls = 0
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {
                "type": "http.request",
                "body": _selected_download_body(app),
                "more_body": False,
            }
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    serving = asyncio.create_task(_run_synthetic_selected_download(app, receive, send))
    await build_started.wait()
    disconnect.set()
    await cleanup_started.wait()
    await asyncio.sleep(0)

    assert not serving.done()
    assert messages == []

    cleanup_release.set()
    await serving

    assert cleaned.is_set()
    assert messages == []


async def test_selected_download_completion_disconnect_race_closes_returned_archive() -> None:
    app, _, _ = _app()
    build_waiting = asyncio.Event()
    receive_waiting = asyncio.Event()
    complete = asyncio.Event()
    staged_file = io.BytesIO(b"archive")

    class RacingArchive(_Archive):
        @override
        async def download(self, request: ArchiveRequest) -> StagedArchive:
            self.download_requests.append(request)
            build_waiting.set()
            await complete.wait()
            return StagedArchive(staged_file, 7)

    app.state.archive = RacingArchive()
    receive_calls = 0
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {
                "type": "http.request",
                "body": _selected_download_body(app),
                "more_body": False,
            }
        receive_waiting.set()
        await complete.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    serving = asyncio.create_task(_run_synthetic_selected_download(app, receive, send))
    await build_waiting.wait()
    await receive_waiting.wait()
    complete.set()
    await serving

    assert staged_file.closed
    assert messages == []


async def test_selected_download_repeated_cancellation_drains_build_cleanup() -> None:
    app, _, _ = _app()
    build_started = asyncio.Event()
    listener_started = asyncio.Event()
    listener_finished = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()

    class BlockingArchive(_Archive):
        @override
        async def download(self, request: ArchiveRequest) -> StagedArchive:
            self.download_requests.append(request)
            build_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cleaned.set()
            raise AssertionError("cancelled build resumed")

    app.state.archive = BlockingArchive()
    receive_calls = 0
    messages: list[Message] = []

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        if receive_calls == 1:
            return {
                "type": "http.request",
                "body": _selected_download_body(app),
                "more_body": False,
            }
        listener_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            listener_finished.set()
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        messages.append(message)

    serving = asyncio.create_task(_run_synthetic_selected_download(app, receive, send))
    await build_started.wait()
    await listener_started.wait()
    serving.cancel()
    await cleanup_started.wait()
    serving.cancel()
    await asyncio.sleep(0)

    assert not serving.done()

    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await serving

    assert cleaned.is_set()
    assert listener_finished.is_set()
    assert messages == []


def _archive_response_scope(*, spec_version: str = "2.4") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/selected/download",
        "raw_path": b"/selected/download",
        "query_string": b"",
        "headers": [],
        "client": None,
        "server": None,
    }


async def test_owned_staged_archive_response_closes_normally() -> None:
    file = io.BytesIO(b"archive")
    response = routes._OwnedStagedArchiveResponse(StagedArchive(file, 7), {})
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    await response(_archive_response_scope(), receive, send)

    assert file.closed
    assert (
        b"".join(
            message.get("body", b"")
            for message in messages
            if message["type"] == "http.response.body"
        )
        == b"archive"
    )


@pytest.mark.parametrize("failure", [RuntimeError("send failed"), asyncio.CancelledError()])
async def test_owned_staged_archive_response_closes_on_send_failure(
    failure: BaseException,
) -> None:
    file = io.BytesIO(b"archive")
    response = routes._OwnedStagedArchiveResponse(StagedArchive(file, 7), {})

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        raise failure

    with pytest.raises(type(failure)):
        await response(_archive_response_scope(), receive, send)

    assert file.closed


async def test_owned_staged_archive_response_preserves_cancellation_during_failure_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class DelayedCloseStagedArchive(StagedArchive):
        @override
        async def aclose(self) -> None:
            cleanup_started.set()
            await cleanup_release.wait()
            await super().aclose()

    file = io.BytesIO(b"archive")
    staged = DelayedCloseStagedArchive(file, 7)
    response = routes._OwnedStagedArchiveResponse(staged, {})
    failure = RuntimeError("send failed")

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        raise failure

    sending = asyncio.create_task(response(_archive_response_scope(), receive, send))
    await cleanup_started.wait()
    sending.cancel()
    await asyncio.sleep(0)
    cleanup_release.set()

    with pytest.raises(asyncio.CancelledError) as raised:
        await sending

    assert raised.value.__cause__ is failure
    assert file.closed
    assert "failure_type=RuntimeError" in caplog.text


async def test_owned_staged_archive_response_closes_on_iteration_failure() -> None:
    class FailingReadFile(io.BytesIO):
        @override
        def read(self, _size: int | None = -1) -> bytes:
            raise RuntimeError("read failed")

    file = FailingReadFile(b"archive")
    response = routes._OwnedStagedArchiveResponse(StagedArchive(file, 7), {})

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        return None

    with pytest.raises(RuntimeError, match="read failed"):
        await response(_archive_response_scope(), receive, send)

    assert file.closed


async def test_owned_staged_archive_response_closes_on_disconnect() -> None:
    file = io.BytesIO(b"archive")
    response = routes._OwnedStagedArchiveResponse(StagedArchive(file, 7), {})

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(_message: Message) -> None:
        await asyncio.sleep(0)

    await response(_archive_response_scope(spec_version="2.3"), receive, send)

    assert file.closed


async def test_owned_staged_archive_response_closes_on_cancellation() -> None:
    file = io.BytesIO(b"archive")
    response = routes._OwnedStagedArchiveResponse(StagedArchive(file, 7), {})
    body_send_started = asyncio.Event()

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            body_send_started.set()
            await asyncio.Event().wait()

    sending = asyncio.create_task(response(_archive_response_scope(), receive, send))
    await body_send_started.wait()
    sending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await sending

    assert file.closed


def test_selected_download_closes_staged_archive_if_response_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive

    def fail_response(_staged: StagedArchive, _headers: dict[str, str]) -> Response:
        raise RuntimeError("response failed")

    monkeypatch.setattr(routes, "_OwnedStagedArchiveResponse", fail_response)
    with TestClient(app) as client:
        response = client.post(
            "/selected/download",
            data=_download_form(app, '["public-1"]', "nested"),
        )

    assert response.status_code == 500
    assert archive.last_file is not None and archive.last_file.closed


def test_book_detail_accepts_exact_selected_return_url() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        selected = client.get("/books/public-1", params={"return_to": "/selected"})
        selected_query = client.get(
            "/books/public-1", params={"return_to": "/selected?unexpected=true"}
        )

    assert _link_href(selected.text, "detail-back-link") == "/selected"
    assert "Back to results" in selected.text
    assert _link_href(selected_query.text, "detail-back-link") == "/"


def test_original_download_headers_body_and_status_mappings(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, _ = _app()
    acquisition: _Acquisition = app.state.acquisition
    with TestClient(app) as client:
        response = client.get("/books/public-1/download")
        acquisition.error = AcquisitionNotFoundError()
        missing = client.get("/books/missing/download")
        acquisition.error = AcquisitionCorruptError()
        corrupt = client.get("/books/public-1/download")
        acquisition.error = AcquisitionSourceIOError()
        source_io = client.get("/books/public-1/download")
        acquisition.error = AcquisitionStoreShutdownError()
        shutdown = client.get("/books/public-1/download")

    assert response.status_code == 200
    assert response.content == b"original"
    assert response.headers["content-type"] == "application/x-fictionbook+xml"
    assert response.headers["content-length"] == "8"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert acquisition.stream.closed
    assert missing.status_code == 404
    assert "AcquisitionNotFoundError" not in missing.text
    assert corrupt.status_code == 500
    assert source_io.status_code == 500
    assert shutdown.status_code == 503
    messages = [record.getMessage() for record in caplog.records]
    assert any("failure_type=AcquisitionCorruptError" in message for message in messages)
    assert any("failure_type=AcquisitionSourceIOError" in message for message in messages)
    assert "public-1" not in " ".join(messages)


@pytest.mark.parametrize("failure", [RuntimeError("send failed"), asyncio.CancelledError()])
async def test_owned_download_response_closes_if_send_fails_before_iteration(
    failure: BaseException,
) -> None:
    stream = _Stream()
    original = AcquiredOriginal("book.fb2", "application/octet-stream", 8, stream, "fb2", _REVISION)
    response = routes._OwnedStreamingResponse(original, {})
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/books/public-1/download",
        "raw_path": b"/books/public-1/download",
        "query_string": b"",
        "headers": [],
        "client": None,
        "server": None,
    }

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def fail_send(_message: Message) -> None:
        raise failure

    with pytest.raises(type(failure)):
        await response(scope, receive, fail_send)

    assert stream.closed
    assert not stream.iterated


async def test_owned_download_cleanup_finishes_despite_repeated_cancellation() -> None:
    class BlockingCloseStream(_Stream):
        def __init__(self) -> None:
            super().__init__()
            self.close_started = asyncio.Event()
            self.close_release = asyncio.Event()

        @override
        async def aclose(self) -> None:
            self.close_started.set()
            await self.close_release.wait()
            self.closed = True

    stream = BlockingCloseStream()
    original = AcquiredOriginal("book.fb2", "application/octet-stream", 8, stream, "fb2", _REVISION)
    response = routes._OwnedStreamingResponse(original, {})
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/books/public-1/download",
        "raw_path": b"/books/public-1/download",
        "query_string": b"",
        "headers": [],
        "client": None,
        "server": None,
    }

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    async def cancelled_send(_message: Message) -> None:
        raise asyncio.CancelledError

    sending = asyncio.create_task(response(scope, receive, cancelled_send))
    await stream.close_started.wait()
    sending.cancel()
    await asyncio.sleep(0)
    sending.cancel()
    await asyncio.sleep(0)
    assert not sending.done()
    stream.close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await sending
    assert stream.closed
    assert not stream.iterated


def test_catalog_page_does_not_load_or_render_management_context() -> None:
    imports = _Imports(active=True)
    app, catalog, _ = _app(imports)
    with TestClient(app) as client:
        page = client.get("/")

    assert page.status_code == 200
    assert "Start with a book you have in mind" in page.text
    assert 'href="/manage"' in page.text
    assert 'id="catalog-statistics"' not in page.text
    assert 'id="operation-status"' not in page.text
    assert 'hx-post="/imports"' not in page.text
    assert catalog.requests == []
    assert catalog.statistics_calls == 0
    assert imports.status_calls == 0
    assert imports.active_calls == 0


def test_manage_page_polls_while_active_import_has_no_persisted_status() -> None:
    imports = _Imports(active=True)
    app, catalog, _ = _app(imports)
    with TestClient(app) as client:
        page = client.get("/manage")
        pending = client.get("/imports/status")
        imports.status = _status(ImportState.RUNNING)
        started = client.get("/imports/status")

    assert page.status_code == 200
    assert "No import has run yet" not in page.text
    assert '<a href="/manage" aria-current="page">Manage</a>' in page.text
    assert catalog.requests == []
    assert "Catalog import is starting" in page.text
    assert "Waiting for the import run" in page.text
    assert 'hx-get="/imports/status"' in page.text
    assert 'class="import-status import-status--pending"' in page.text
    assert 'role="status"' in page.text
    assert "Catalog import is starting" in pending.text
    assert 'hx-get="/imports/status"' in pending.text
    assert "<dt>Imported</dt><dd>2</dd>" in started.text


def test_manage_page_polls_past_terminal_status_during_new_import_startup() -> None:
    imports = _Imports(_status(ImportState.SUCCEEDED, run_id=7), active=True)
    app, _, _ = _app(imports)
    with TestClient(app) as client:
        management = client.get("/manage")

    assert management.status_code == 200
    assert 'class="import-status import-status--pending"' in management.text
    assert "Catalog import is starting" in management.text
    assert "Waiting for the import run" in management.text
    assert 'hx-get="/imports/status?after_run_id=7"' in management.text
    assert "<dt>Imported</dt><dd>2</dd>" not in management.text
    assert "Completed" not in management.text


def test_manage_statistics_race_keeps_retryable_management_shell() -> None:
    app, catalog, _ = _app()
    catalog.statistics_failures_remaining = 1
    with TestClient(app) as client:
        management = client.get("/manage")

    assert management.status_code == 503
    assert '<a href="/manage" aria-current="page">Manage</a>' in management.text
    assert 'role="alert"' in management.text
    assert "Statistics unavailable" in management.text
    assert '<a href="/manage">Refresh</a>' in management.text
    assert 'hx-post="/imports"' in management.text
    assert 'hx-post="/imports/force"' in management.text
    assert 'hx-post="/database/vacuum"' in management.text
    assert 'id="operation-status"' in management.text
    assert "No import has run yet" in management.text
    assert 'id="catalog-statistics"' not in management.text
    assert catalog.statistics_calls == 1


def test_failed_import_status_is_an_assertive_alert() -> None:
    imports = _Imports(
        _status(
            ImportState.FAILED,
            error_summary="Could not read the configured catalog source",
        )
    )
    app, _, _ = _app(imports)
    with TestClient(app) as client:
        management = client.get("/manage")

    assert management.status_code == 200
    assert 'class="import-status import-status--failed"' in management.text
    assert 'role="alert"' in management.text
    assert 'aria-live="assertive"' in management.text
    assert "Could not read the configured catalog source" in management.text


def test_import_status_polls_only_while_running() -> None:
    imports = _Imports(_status(ImportState.RUNNING))
    app, _, _ = _app(imports)
    with TestClient(app) as client:
        management = client.get("/manage")
        running = client.get("/imports/status")
        imports.status = _status(ImportState.SUCCEEDED)
        terminal = client.get("/imports/status")

    assert 'hx-get="/imports/status"' in management.text
    assert 'class="import-status import-status--running"' in management.text
    assert 'hx-get="/imports/status"' in running.text
    assert 'hx-trigger="every 2s"' in running.text
    assert 'class="import-status import-status--running"' in running.text
    assert 'role="status"' in running.text
    assert "<dt>Imported</dt><dd>2</dd>" in running.text
    assert 'hx-get="/imports/status"' not in terminal.text
    assert 'class="import-status import-status--succeeded"' in terminal.text
    assert "Completed" in terminal.text


def test_manual_import_requires_csrf_and_reports_current_run() -> None:
    imports = _Imports(_status(ImportState.RUNNING))
    app, _, _ = _app(imports)
    with TestClient(app) as client:
        csrf_token = _csrf_token(app)
        missing = client.post("/imports")
        invalid = client.post("/imports", headers={"X-CSRF-Token": "wrong"})
        expired = client.post(
            "/imports",
            headers={"X-CSRF-Token": issue_csrf_token(app.state.csrf_key, now=0)},
        )
        accepted = client.post("/imports", headers={"X-CSRF-Token": csrf_token})
        forced = client.post("/imports/force", headers={"X-CSRF-Token": csrf_token})
        imports.active = True
        pending = client.get("/imports/status?after_run_id=1")
        imports.status = _status(ImportState.RUNNING, run_id=2)
        started = client.get("/imports/status?after_run_id=1")
        imports.status = _status(ImportState.SUCCEEDED, run_id=2)
        terminal = client.get("/imports/status?after_run_id=1")
        imports.accept = False
        already_running = client.post("/imports", headers={"X-CSRF-Token": csrf_token})

    assert missing.status_code == invalid.status_code == expired.status_code == 403
    assert routes._CSRF_ERROR_MESSAGE in missing.text
    assert routes._CSRF_ERROR_MESSAGE in invalid.text
    assert routes._CSRF_ERROR_MESSAGE in expired.text
    assert missing.headers["X-SOPDS-CSRF-Expired"] == "true"
    assert invalid.headers["X-SOPDS-CSRF-Expired"] == "true"
    assert expired.headers["X-SOPDS-CSRF-Expired"] == "true"
    assert accepted.status_code == 202
    assert forced.status_code == 202
    assert "Import check is starting" in accepted.text
    assert "Force import is starting" in forced.text
    assert "<dt>Imported</dt><dd>2</dd>" not in accepted.text
    assert "after_run_id=1" in accepted.text
    assert "Waiting for the import run" in pending.text
    assert "<dt>Imported</dt><dd>2</dd>" in started.text
    assert "after_run_id=1" in started.text
    assert terminal.headers["HX-Trigger"] == "catalogChanged"
    assert already_running.status_code == 200
    assert "already running" in already_running.text
    assert imports.started == [False, True, False]


def test_unchanged_import_stops_polling_and_vacuum_refreshes_statistics() -> None:
    imports = _Imports(_status(ImportState.SUCCEEDED))
    app, _, _ = _app(imports)
    with TestClient(app) as client:
        csrf_token = _csrf_token(app)
        unchanged = client.get("/imports/status?after_run_id=1")
        missing_csrf = client.post("/database/vacuum")
        vacuumed = client.post("/database/vacuum", headers={"X-CSRF-Token": csrf_token})
        imports.vacuumed = False
        busy = client.post("/database/vacuum", headers={"X-CSRF-Token": csrf_token})

    assert unchanged.status_code == 200
    assert "No catalog changes found" in unchanged.text
    assert 'hx-get="/imports/status' not in unchanged.text
    assert missing_csrf.status_code == 403
    assert vacuumed.status_code == 200
    assert "Database VACUUM completed" in vacuumed.text
    assert 'role="status" aria-live="polite"' in vacuumed.text
    assert vacuumed.headers["HX-Trigger"] == "catalogChanged"
    assert "Database size" not in vacuumed.text
    assert busy.status_code == 200
    assert "VACUUM skipped" in busy.text
    assert 'role="alert" aria-live="assertive"' in busy.text
    assert "HX-Trigger" not in busy.headers
    assert imports.vacuum_calls == 2
