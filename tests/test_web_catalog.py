"""Web adapter tests for catalog rendering, status polling, and manual import CSRF."""

import asyncio
import html
import re
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import override
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from sopds.acquisition.contracts import (
    AcquiredOriginal,
    AcquisitionCorruptError,
    AcquisitionNotFoundError,
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
                        if request.query == "hidden"
                        else BookAvailability.MISSED
                        if request.query == "missed"
                        else BookAvailability.ACTIVE
                    ),
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
    app.state.csrf_token = secrets.token_urlsafe(32)
    static = Path(routes.__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")
    app.include_router(routes.router)
    return app, catalog, import_provider


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
        )
        in catalog.requests
    )
    assert detail.status_code == 200
    assert '<a href="/" aria-current="page">Catalog</a>' in detail.text
    assert "/static/css/app.css" in detail.text
    assert "/static/vendor/htmx/htmx-2.0.10.min.js" in detail.text
    assert 'hx-get="/health-fragment"' in detail.text
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
    assert CatalogRequest(author="Тестов,Тест,") in catalog.requests
    assert CatalogRequest(series="Series") in catalog.requests
    assert catalog.requests[0] == CatalogRequest(
        query="book",
        search_field=SearchField.TITLE,
        language="en",
        genre="sf",
        original_format="fb2",
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
    assert 'hx-get="/health-fragment"' in response.text
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
        CatalogRequest(query="hidden", include_missed=True, include_hidden=True) in catalog.requests
    )
    assert (
        CatalogRequest(query="missed", include_missed=True, include_hidden=True) in catalog.requests
    )


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
    assert shutdown.status_code == 503
    messages = [record.getMessage() for record in caplog.records]
    assert any("failure_type=AcquisitionCorruptError" in message for message in messages)
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
    csrf_token = app.state.csrf_token
    with TestClient(app) as client:
        missing = client.post("/imports")
        invalid = client.post("/imports", headers={"X-CSRF-Token": "wrong"})
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

    assert missing.status_code == 403
    assert invalid.status_code == 403
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
    csrf_token = app.state.csrf_token
    with TestClient(app) as client:
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
