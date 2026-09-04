"""Web adapter tests for catalog rendering, status polling, and manual import CSRF."""

import asyncio
import html
import io
import json
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, override
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
    OriginalDescription,
    SourceRevision,
)
from sopds.catalog.contracts import (
    BookAvailability,
    CatalogBook,
    CatalogFilters,
    CatalogInputError,
    CatalogPage,
    CatalogRequest,
    CatalogStatistics,
    FilterOption,
    SearchField,
)
from sopds.conversion.adapters import (
    EpubToAzw3Converter,
    Fb2ToAzw3Converter,
    Fb2ToEpubConverter,
)
from sopds.conversion.cache import ArtifactCache
from sopds.conversion.contracts import (
    ConversionResult,
    ConversionShutdownError,
    ConversionSourceError,
    ConversionTimeoutError,
    ConverterExecutionError,
    InvalidConversionOutputError,
    SourceChangedError,
    SourceUnavailableError,
    UnsupportedConversionError,
)
from sopds.conversion.registry import ConverterRegistry
from sopds.conversion.service import ConversionService
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
        self.detail_size = 126_000
        self.result_size = 126_000
        self.result_title: str | None = None
        self.truncated = True
        self.original_format = "fb2"
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
        title = self.result_title or (
            "Очень длинное название книги для проверки многоязычного каталога"
            if request.query == "many-authors"
            else "A Book"
        )
        return CatalogPage(
            books=(
                CatalogBook(
                    public_id="public-1",
                    title=title,
                    authors=authors,
                    series="Series",
                    series_number="1",
                    language=None if request.query == "sparse-metadata" else "en",
                    original_format=self.original_format,
                    size=self.result_size,
                    member_filename="private/archive/member.fb2",
                    libid="private-library-id",
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
            next_cursor="next-token" if self.truncated else None,
        )

    async def details(
        self,
        public_id: str,
        *,
        include_missed: bool = False,
        include_hidden: bool = False,
    ) -> CatalogBook | None:
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
        return CatalogBook(
            public_id=public_id,
            title=self.detail_title,
            authors=(
                "Тестов,Тест,",
                " Примеров,Пример,Примерович",
            ),
            genres=(("sf", "Science fiction"),),
            series="Series",
            series_number="1",
            size=self.detail_size,
            libid=self.detail_libid,
            published_date=self.detail_published_date,
            language="en",
            original_format=self.original_format,
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
        self.revision = _REVISION

    async def acquire(self, public_id: str) -> AcquiredOriginal:
        if self.error is not None:
            raise self.error
        return AcquiredOriginal(
            filename="Книга.fb2",
            media_type="application/x-fictionbook+xml",
            content_length=len(self.stream.body),
            stream=self.stream,
            source_format="fb2",
            source_revision=self.revision,
        )


class _Conversion:
    def __init__(self) -> None:
        self.error: Exception | None = None
        self.calls: list[tuple[str, str]] = []
        self.last_stream: _Stream | None = None

    async def convert(self, public_id: str, target_format: str) -> ConversionResult:
        self.calls.append((public_id, target_format))
        if self.error is not None:
            raise self.error
        self.last_stream = _Stream(b"converted")
        return ConversionResult(
            filename=f"Книга.{target_format}",
            media_type=(
                "application/epub+zip"
                if target_format == "epub"
                else "application/vnd.amazon.ebook"
            ),
            content_length=len(self.last_stream.body),
            stream=self.last_stream,
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
        summary = CatalogBook(
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
                supported_formats=("epub", "azw3"),
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


def _catalog_payload(markup: str) -> dict[str, Any]:
    match = re.search(
        r'<script id="catalog-result-payload" type="application/json" '
        r"data-catalog-payload>(.*?)</script>",
        markup,
        re.S,
    )
    assert match is not None
    payload = json.loads(match.group(1))
    assert isinstance(payload, dict)
    return payload


def _detail_href(markup: str) -> str:
    payload = _catalog_payload(markup)
    books = payload["books"]
    assert isinstance(books, list) and books
    detail_url = books[0]["detailUrl"]
    assert isinstance(detail_url, str)
    return detail_url


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
    app.state.converter_registry = ConverterRegistry(
        (Fb2ToEpubConverter(), Fb2ToAzw3Converter(), EpubToAzw3Converter())
    )
    app.state.conversion = _Conversion()
    app.state.csrf_key = b"c" * 32
    static = Path(routes.__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static), name="static")
    app.include_router(routes.router)
    return app, catalog, import_provider


def _csrf_token(app: FastAPI) -> str:
    return issue_csrf_token(app.state.csrf_key)


def _download_form(
    app: FastAPI, ids: str, preset: str, target_format: str | None = None
) -> dict[str, str]:
    fields = {"ids": ids, "preset": preset, "csrf_token": _csrf_token(app)}
    if target_format is not None:
        fields["format"] = target_format
    return fields


def _csrf_form_suffix(app: FastAPI) -> str:
    return "&" + urlencode({"csrf_token": _csrf_token(app)})


def test_full_page_and_fragment_serve_capped_catalog_payload() -> None:
    app, catalog, _ = _app()
    with TestClient(app) as client:
        page = client.get("/?q=book&search_field=title&language=en&genre=sf&original_format=fb2")
        fragment = client.get("/catalog-fragment?q=book&search_field=author&language=en&genre=sf")
        ignored_cursor = client.get("/?q=book&cursor=next-token")
        detail = client.get("/books/public-1")
        missing = client.get("/books/missing")
        author_page = client.get("/", params={"q": "Тестов Тест", "search_field": "author"})
        series_page = client.get("/", params={"q": "Series", "search_field": "series"})

    assert page.status_code == 200
    payload = _catalog_payload(page.text)
    assert payload["truncated"] is True
    assert payload["books"] == [
        {
            "publicId": "public-1",
            "title": "A Book",
            "titleSortKey": "a book",
            "authors": [
                {
                    "raw": "Тестов,Тест,",
                    "display": "Тестов Тест",
                    "sortKey": "тестов,тест,",
                    "scopeUrl": (
                        "/?q=%D0%A2%D0%B5%D1%81%D1%82%D0%BE%D0%B2+%D0%A2%D0%B5%D1%81%D1%82"
                        "&search_field=author&language=en&genre=sf&original_format=fb2"
                    ),
                },
                {
                    "raw": " Примеров,Пример,Примерович",
                    "display": "Примеров Пример Примерович",
                    "sortKey": " примеров,пример,примерович",
                    "scopeUrl": (
                        "/?q=%D0%9F%D1%80%D0%B8%D0%BC%D0%B5%D1%80%D0%BE%D0%B2+"
                        "%D0%9F%D1%80%D0%B8%D0%BC%D0%B5%D1%80+"
                        "%D0%9F%D1%80%D0%B8%D0%BC%D0%B5%D1%80%D0%BE%D0%B2%D0%B8%D1%87"
                        "&search_field=author&language=en&genre=sf&original_format=fb2"
                    ),
                },
            ],
            "series": {
                "name": "Series",
                "sortKey": "series",
                "number": "1",
                "scopeUrl": (
                    "/?q=Series&search_field=series&language=en&genre=sf&original_format=fb2"
                ),
            },
            "language": "en",
            "sourceFormat": {"key": "fb2", "label": "FB2"},
            "size": 126_000,
            "sizeLabel": "123 KB",
            "publishedDate": "2024-02-03",
            "availability": "active",
            "selectable": True,
            "downloadable": True,
            "detailUrl": "/books/public-1",
            "readUrl": "/books/public-1/read",
            "originalDownload": {"url": "/books/public-1/download", "label": "FB2"},
            "conversions": [
                {"url": "/books/public-1/download/epub", "label": "EPUB"},
                {"url": "/books/public-1/download/azw3", "label": "AZW3"},
            ],
        }
    ]
    assert "1 loaded · More match — refine search" in page.text
    assert 'aria-label="Result view"' in page.text
    assert 'data-catalog-view="flat" aria-pressed="true"' in page.text
    assert page.text.count("data-catalog-result-view") == 1
    assert "JavaScript is required to display catalog results" in page.text
    assert "result-row--catalog" not in page.text
    assert "catalog-pagination" not in page.text
    assert "next-token" not in page.text
    assert "/static/navigation.js" in page.text
    assert "/static/book_sorting.js" in page.text
    assert "/static/catalog.js" in page.text
    assert "private/archive/member.fb2" not in page.text
    assert "private-library-id" not in page.text
    assert fragment.status_code == 200
    assert fragment.headers["HX-Push-Url"] == (
        "/?q=book&search_field=author&language=en&genre=sf&original_format="
    )
    assert "<html" not in fragment.text
    assert ignored_cursor.status_code == 200
    assert author_page.status_code == 200
    assert series_page.status_code == 200
    assert all(request.cursor is None for request in catalog.requests)
    assert (
        CatalogRequest(query="Тестов Тест", search_field=SearchField.AUTHOR, page_size=1_000)
        in catalog.requests
    )
    assert (
        CatalogRequest(query="Series", search_field=SearchField.SERIES, page_size=1_000)
        in catalog.requests
    )
    assert catalog.requests[0] == CatalogRequest(
        query="book",
        search_field=SearchField.TITLE,
        language="en",
        genre="sf",
        original_format="fb2",
        page_size=1_000,
    )
    assert detail.status_code == 200
    assert _link_href(detail.text, "detail-back-link") == "/"
    assert "Back to results" in detail.text
    assert "return_to" not in detail.text
    assert missing.status_code == 404


@pytest.mark.parametrize(
    ("source_format", "size", "readable"),
    [
        ("fb2", 64 * 1024 * 1024, True),
        ("epub", 64 * 1024 * 1024, True),
        ("fb2", 64 * 1024 * 1024 + 1, False),
        ("azw3", 1, False),
    ],
)
def test_catalog_payload_enforces_result_reader_eligibility(
    source_format: str,
    size: int,
    readable: bool,
) -> None:
    app, catalog, _ = _app()
    catalog.original_format = source_format
    catalog.result_size = size

    with TestClient(app) as client:
        response = client.get("/?q=book")

    book = _catalog_payload(response.text)["books"][0]
    assert (book["readUrl"] is not None) is readable
    assert book["downloadable"] is True
    assert book["originalDownload"]["url"] == "/books/public-1/download"


def test_catalog_payload_is_script_safe_and_complete_status_is_truthful() -> None:
    app, catalog, _ = _app()
    catalog.result_title = '</script><img src=x onerror="alert(1)"> Ёж'
    catalog.truncated = False

    with TestClient(app) as client:
        response = client.get("/?q=hostile")

    assert response.status_code == 200
    assert "1 book loaded." in response.text
    assert "More books match" not in response.text
    assert "</script><img" not in response.text
    assert r"\u003c/script\u003e\u003cimg" in response.text
    book = _catalog_payload(response.text)["books"][0]
    assert book["title"] == catalog.result_title
    assert book["titleSortKey"] == '</script><img src=x onerror="alert(1)"> еж'
    assert _catalog_payload(response.text)["truncated"] is False


def test_browser_message_payloads_are_compact_localized_and_attribute_escaped() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.get("/?q=book", headers={"Accept-Language": "ru"})

    assert response.status_code == 200
    assert 'data-ui-locale="ru"' in response.text
    assert response.text.count('data-history-locale="ru"') == 1
    catalog_match = re.search(r'data-catalog-messages="([^"]*)"', response.text)
    selection_match = re.search(r'data-selection-messages="([^"]*)"', response.text)
    assert catalog_match is not None
    assert selection_match is not None
    catalog_messages = json.loads(html.unescape(catalog_match.group(1)))
    selection_messages = json.loads(html.unescape(selection_match.group(1)))
    assert catalog_messages["unknownAuthor"] == "Неизвестный автор"
    assert catalog_messages["filteredLoaded"]["many"] == ("Загружено {count} книг из {total}.")
    assert selection_messages["couldNotLoadPreview"] == (
        "Не удалось загрузить предпросмотр выбранного."  # noqa: RUF001
    )
    assert "<script" not in catalog_match.group(1)
    assert "<script" not in selection_match.group(1)


def test_catalog_no_results_preserves_server_rendered_recovery_copy() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.get("/?q=none")

    assert response.status_code == 200
    assert "No books found" in response.text
    assert "Try a broader search" in response.text
    assert "data-catalog-root" not in response.text
    assert "catalog-result-payload" not in response.text


def test_manage_page_groups_counts_localizes_times_and_preserves_action_contracts() -> None:
    imports = _Imports(
        _status(
            ImportState.SUCCEEDED,
            records_read=703_695,
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
        ("Read", "703 695"),
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
    assert "Application is healthy" not in response.text
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
        "/?q=invalid&search_field=title&language=en&genre=sf&original_format=fb2"
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
    assert active.headers["HX-Push-Url"] == (
        "/?q=book&search_field=title&language=en&genre=sf&original_format=fb2"
        "&include_missed=true&include_hidden=true"
    )
    form = active.text[active.text.index("<form") : active.text.index("</form>")]
    assert 'id="catalog-search-form"' in form
    assert 'hx-swap-oob="outerHTML"' in form
    assert 'name="q" type="search" value="book"' in form
    assert '<option value="title" selected>Title</option>' in form
    assert '<option value="en" selected>en</option>' in form
    assert '<option value="sf" selected>Science fiction</option>' in form
    assert '<option value="fb2" selected>fb2</option>' in form
    assert 'name="author"' not in form
    assert 'name="series"' not in form
    assert "Searching within" not in form
    assert 'name="include_missed" value="true" checked' in form
    assert 'name="include_hidden" value="true" checked' in form
    assert "catalog-more-filters" not in form
    assert "Include missing" in form
    assert "Include hidden" in form
    assert (
        'id="catalog-clear-action" class="catalog-clear" href="/" '
        'data-catalog-criteria-link aria-label="Clear search and filters">Clear all</a>' in form
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


def test_metadata_search_links_preserve_optional_missed_and_hidden_filters() -> None:
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
    hidden_book = _catalog_payload(page.text)["books"][0]
    assert hidden_book["availability"] == "hidden"
    assert hidden_book["detailUrl"] == ("/books/public-1?include_missed=true&include_hidden=true")
    assert hidden_book["readUrl"] == (
        "/books/public-1/read?include_missed=true&include_hidden=true"
    )
    assert parse_qs(urlsplit(hidden_book["authors"][0]["scopeUrl"]).query) == {
        "q": ["Тестов Тест"],
        "search_field": ["author"],
        "include_missed": ["true"],
        "include_hidden": ["true"],
    }
    assert parse_qs(urlsplit(hidden_book["series"]["scopeUrl"]).query) == {
        "q": ["Series"],
        "search_field": ["series"],
        "include_missed": ["true"],
        "include_hidden": ["true"],
    }
    assert fragment.status_code == 200
    missed_book = _catalog_payload(fragment.text)["books"][0]
    assert missed_book["availability"] == "missed"
    assert missed_book["selectable"] is False
    assert missed_book["downloadable"] is False
    assert missed_book["readUrl"] is None
    assert missed_book["originalDownload"] is None
    assert missed_book["conversions"] == []
    assert fragment.headers["HX-Push-Url"].endswith("&include_missed=true&include_hidden=true")
    assert (
        CatalogRequest(query="hidden", include_missed=True, include_hidden=True, page_size=1_000)
        in catalog.requests
    )
    assert (
        CatalogRequest(query="missed", include_missed=True, include_hidden=True, page_size=1_000)
        in catalog.requests
    )


def test_unavailable_hidden_book_has_no_catalog_or_detail_download_action() -> None:
    app, catalog, _ = _app()
    catalog.detail_downloadable = False
    with TestClient(app) as client:
        results = client.get("/?q=hidden-unavailable&include_hidden=true")
        detail = client.get("/books/public-1?include_hidden=true")

    assert results.status_code == 200
    book = _catalog_payload(results.text)["books"][0]
    assert book["availability"] == "hidden"
    assert book["selectable"] is False
    assert book["originalDownload"] is None
    assert book["conversions"] == []
    assert detail.status_code == 200
    assert "Original file unavailable" in detail.text
    assert 'href="/books/public-1/download"' not in detail.text


def test_result_detail_link_is_clean_and_preserves_only_availability_flags() -> None:
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

    assert detail_href == "/books/public-1?include_missed=true&include_hidden=true"
    assert "return_to" not in detail_href
    assert detail.status_code == 200
    assert _link_href(detail.text, "detail-back-link") == "/"
    assert "Back to results" in detail.text
    assert catalog.detail_requests[-1] == (True, True)
    assert (
        'href="/?q=%D0%A2%D0%B5%D1%81%D1%82%D0%BE%D0%B2%20%D0%A2%D0%B5%D1%81%D1%82'
        '&amp;search_field=author&amp;include_missed=true&amp;include_hidden=true"' in detail.text
    )
    assert (
        'href="/?q=Series&amp;search_field=series&amp;include_missed=true'
        '&amp;include_hidden=true"' in detail.text
    )


def test_book_detail_ignores_obsolete_return_context() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        response = client.get(
            "/books/public-1",
            params={"return_to": "https://example.invalid/?q=book"},
        )

    assert response.status_code == 200
    assert _link_href(response.text, "detail-back-link") == "/"
    assert "data-detail-back" in response.text
    assert "Back to results" in response.text
    assert "example.invalid" not in response.text


def test_book_detail_read_action_is_secondary_and_omits_return_context() -> None:
    app, catalog, _ = _app()
    return_to = "/?q=book&search_field=title&cursor=opaque%2Ftoken"
    with TestClient(app) as client:
        active = client.get("/books/public-1", params={"return_to": return_to})
        hidden = client.get(
            "/books/public-1",
            params={
                "include_missed": "true",
                "include_hidden": "true",
                "return_to": return_to,
            },
        )

        catalog.original_format = "epub"
        epub = client.get("/books/public-1")

        catalog.original_format = "azw3"
        unsupported = client.get("/books/public-1")

        catalog.original_format = "fb2"
        missed = client.get("/books/public-1?include_missed=true")

        catalog.detail_downloadable = False
        unavailable = client.get("/books/public-1")

        catalog.detail_downloadable = True
        catalog.detail_size = 64 * 1024 * 1024 + 1
        over_limit = client.get("/books/public-1")

    active_href = _link_href(active.text, "detail-read-link")
    hidden_href = _link_href(hidden.text, "detail-read-link")
    assert active_href is not None
    assert urlsplit(active_href).path == "/books/public-1/read"
    assert parse_qs(urlsplit(active_href).query) == {}
    assert parse_qs(urlsplit(hidden_href).query) == {
        "include_missed": ["true"],
        "include_hidden": ["true"],
    }
    for response in (active, hidden, epub, over_limit):
        assert 'target="_blank" rel="noopener noreferrer">Read</a>' in response.text
        assert response.text.index("book-detail__download-action") < response.text.index(
            "book-detail__read-action"
        )
    for response in (unsupported, missed, unavailable):
        assert 'data-testid="detail-read-link"' not in response.text


def test_reader_route_rejects_ineligible_books_and_honors_availability_scopes() -> None:
    app, catalog, _ = _app()
    with TestClient(app) as client:
        missing = client.get("/books/missing/read")
        russian_missing = client.get("/books/missing/read", headers={"Accept-Language": "ru"})

        catalog.detail_downloadable = False
        unavailable = client.get("/books/public-1/read")

        catalog.detail_downloadable = True
        missed = client.get("/books/public-1/read?include_missed=true")

        catalog.original_format = "azw3"
        unsupported = client.get("/books/public-1/read")

        catalog.original_format = "epub"
        epub = client.get("/books/public-1/read")
        hidden = client.get("/books/public-1/read?include_hidden=true")

    for response in (missing, russian_missing, unavailable, missed, unsupported):
        assert response.status_code == 404
        assert response.headers["content-security-policy"] == routes._READER_CSP
        assert "book_reader.html" not in response.text
    assert russian_missing.json() == {"detail": "Book not found"}
    assert "vary" not in russian_missing.headers
    assert "content-language" not in russian_missing.headers
    assert epub.status_code == 200
    assert 'data-source-format="epub"' in epub.text
    assert hidden.status_code == 200
    assert catalog.detail_requests == [
        (False, False),
        (False, False),
        (False, False),
        (True, False),
        (False, False),
        (False, False),
        (False, True),
    ]


def test_reader_shell_is_standalone_and_preserves_availability_context() -> None:
    app, catalog, _ = _app()
    with TestClient(app) as client:
        response = client.get(
            "/books/public-1/read",
            params={"include_hidden": "true"},
        )

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == routes._READER_CSP
    for directive in (
        "default-src 'none'",
        "script-src 'self'",
        "script-src-attr 'none'",
        "style-src 'self' 'unsafe-inline' blob:",
        "img-src data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-src blob:",
        "worker-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    ):
        assert directive in response.headers["content-security-policy"]
    assert "data-reader-root" in response.text
    assert '<html lang="en">' in response.text
    assert 'data-ui-locale="en"' in response.text
    assert 'data-reader-locale="en"' in response.text
    assert 'data-reader-messages="' in response.text
    assert 'data-public-id="public-1"' in response.text
    assert 'data-source-format="fb2"' in response.text
    assert 'data-source-url="/books/public-1/download"' in response.text
    assert 'data-reader-state="loading" role="status" aria-live="polite"' in response.text
    assert (
        'data-reader-state="reader" data-reader-mode="scroll" aria-label="Book reader" hidden'
        in response.text
    )
    assert 'data-reader-toolbar role="toolbar" aria-label="Reading controls"' in response.text
    assert 'data-reader-visible-state="loading"' in response.text
    assert response.text.count("data-reader-toolbar-menu-toggle") == 2
    assert 'aria-label="Text size"' in response.text
    assert response.text.count('aria-label="Interface language"') == 3
    assert "reader-language-icon" in response.text
    assert "reader-language-control--menu" in response.text
    assert "reader-mode-icon--pages" in response.text
    assert "reader-mode-icon--scroll" in response.text
    assert "data-reader-mode-toggle" in response.text
    assert response.text.index("data-reader-mode-toggle") < response.text.index(
        "reader-toolbar-menu--language"
    )
    assert "data-reader-page-dock" in response.text
    assert "data-reader-edge-left" in response.text
    assert (
        'data-reader-book-position aria-label="Book position" aria-orientation="vertical"'
        in response.text
    )
    assert "data-reader-seek-preview" in response.text
    assert 'data-reader-contents aria-labelledby="reader-contents-title"' in response.text
    assert 'data-reader-contents-close aria-label="Close contents"' in response.text
    assert 'data-reader-state="error" role="alert" hidden' in response.text
    assert response.text.count("<script") == 2
    assert '<script defer src="/static/locale.js"></script>' in response.text
    assert '<script type="module" src="/static/reader/app.js"></script>' in response.text
    assert response.text.count("<link") == 1
    assert '<link rel="stylesheet" href="/static/css/reader.css">' in response.text
    assert not re.search(r"<script(?![^>]+src=)", response.text)
    assert "htmx" not in response.text.casefold()
    assert "selection.js" not in response.text
    assert "Application is healthy" not in response.text
    assert _link_href(response.text, "reader-download") == "/books/public-1/download"
    retry = _link_href(response.text, "reader-retry")
    detail = _link_href(response.text, "reader-back")
    assert parse_qs(urlsplit(retry).query) == {"include_hidden": ["true"]}
    assert parse_qs(urlsplit(detail).query) == {"include_hidden": ["true"]}
    assert urlsplit(retry).path == "/books/public-1/read"
    assert urlsplit(detail).path == "/books/public-1"
    assert catalog.detail_requests == [(False, True)]


def test_reader_static_assets_expose_only_the_local_reader_entry_contract() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        javascript = client.get("/static/reader/app.js")
        reader_i18n = client.get("/static/reader/i18n.js")
        stylesheet = client.get("/static/css/reader.css")
        book_adapter = client.get("/static/reader/book.js")
        foliate_fb2 = client.get("/static/vendor/foliate/fb2.js")
        policy = client.get("/static/reader/policy.js")
        state = client.get("/static/reader/state.js")
        paginator = client.get("/static/vendor/foliate/paginator.js")

    assert javascript.status_code == 200
    assert reader_i18n.status_code == 200
    assert stylesheet.status_code == 200
    assert book_adapter.status_code == 200
    assert foliate_fb2.status_code == 200
    assert policy.status_code == 200
    assert state.status_code == 200
    assert paginator.status_code == 200
    assert "import '../vendor/foliate/view.js'" in javascript.text
    assert "import { openPublication } from './book.js'" in javascript.text
    assert "from './i18n.js'" in javascript.text
    assert "safeReaderErrorMessage(readerI18n, error)" in javascript.text
    assert "new Intl.NumberFormat(locale" in reader_i18n.text
    assert "error?.name === 'PublicationError'" in reader_i18n.text
    assert "view.renderer.setAttribute('max-column-count', '1')" in javascript.text
    assert "view.renderer.setStyles" in javascript.text
    assert "font-size: ${scale}em !important;" in javascript.text
    assert "await publication.destroy()" in javascript.text
    assert "event.stopImmediatePropagation()" in javascript.text
    assert "getReaderMode" in javascript.text
    assert "setReaderMode" in javascript.text
    assert "modeToggleLabel.textContent = control.text" in javascript.text
    assert "view.renderer.goTo(resolved)" in javascript.text
    assert "view.renderer.inert = true" in javascript.text
    assert "event.code === 'Space'" in javascript.text
    assert "'Spacebar'" in javascript.text
    assert "activeView.renderer.nextScreen()" in javascript.text
    assert "await view.goToFraction(fraction)" in javascript.text
    assert "bookPositionPointerSeeking" in javascript.text
    assert "finishBookPositionPointerInteraction" in javascript.text
    assert "bookPosition.addEventListener('lostpointercapture'" in javascript.text
    assert "if (restoreFocus) bookPosition.focus()" in javascript.text
    assert "BOOK_POSITION_MAX = 10_000" in javascript.text
    assert "configureBookPositionMarkers" in javascript.text
    assert "previewText = label" in javascript.text
    assert "setAttribute('aria-current', 'location')" in javascript.text
    assert "data-reader-current-parent" in javascript.text
    assert "requestAnimationFrame(centerCurrentContentsEntry)" in javascript.text
    assert "contentsCloseButton.addEventListener('click'" in javascript.text
    assert "sopds.reader.v1" in state.text
    assert "export const getReaderMode" in state.text
    assert "export const setReaderMode" in state.text
    assert "foliate-view" in stylesheet.text
    assert "prefers-color-scheme: dark" in stylesheet.text
    assert "grid-template-columns: minmax(0, 1fr) auto repeat(4, 2.75rem)" in stylesheet.text
    assert ".reader-toolbar-menu[data-open] .reader-toolbar-popover" in stylesheet.text
    assert ".reader-mode-icon--pages" in stylesheet.text
    assert ".reader-language-icon" in stylesheet.text
    assert 'data-reader-mode="scroll"] [data-reader-surface] {\n    padding-right: 1rem' in (
        stylesheet.text
    )
    assert ".reader-book-scrollbar {\n    width: 1rem" in stylesheet.text
    toolbar_css = stylesheet.text.split("[data-reader-toolbar] {", 1)[1].split("}", 1)[0]
    assert "z-index: 4" in toolbar_css
    assert "@media (max-width: 24rem)" in stylesheet.text
    assert "@media (max-width: 14rem)" in stylesheet.text
    assert "max-height: 50vh" in stylesheet.text
    assert "overflow-y: auto" in stylesheet.text
    assert "overscroll-behavior-y: contain" in stylesheet.text
    assert 'button[aria-current="location"]' in stylesheet.text
    assert "[data-reader-current-parent]" in stylesheet.text
    assert "[data-reader-contents-close]" in stylesheet.text
    contents_css = stylesheet.text.split("[data-reader-contents] {", 1)[1].split("}", 1)[0]
    assert "overflow: hidden" in contents_css
    assert "data-reader-mode-toggle" in stylesheet.text
    assert ".reader-language-control" in stylesheet.text
    assert "data-reader-book-position" in stylesheet.text
    assert "writing-mode: vertical-lr" in stylesheet.text
    assert "-webkit-tap-highlight-color: transparent" in stylesheet.text
    assert "grid-column: 4 / 6" not in stylesheet.text
    assert 'flow="scrolled"' in paginator.text
    assert "#adjacentIndex(direction)" in paginator.text
    assert "async nextScreen()" in paginator.text
    assert "-webkit-overflow-scrolling: touch" in paginator.text
    assert "scrollbar-width: none" in paginator.text
    assert "if (!this.#touchBoundary) return" in paginator.text
    assert "if (logicalDelta) this.#scrollByLogical(logicalDelta)" not in paginator.text
    assert "this.#container.addEventListener('wheel', this.#boundWheel" in paginator.text
    assert "this.addEventListener('wheel'" not in paginator.text
    assert "min-width: 20rem" not in stylesheet.text
    assert 'url("../fonts/IBMPlexSans-Regular.woff2")' in stylesheet.text

    css_allowlist = book_adapter.text.split("const CSS_PROPERTIES", 1)[1].split("])\n", 1)[0]
    assert "'font-size'" not in css_allowlist
    assert "(meta.getAttribute('property') ?? '').toLowerCase()" in book_adapter.text
    assert "(itemref.getAttribute('linear') ?? '').toLowerCase()" in book_adapter.text
    assert "(sourceElement.getAttribute('class') ?? '')" in book_adapter.text
    assert "(node.getAttribute('rel') ?? '').toLowerCase()" in book_adapter.text
    assert "createRasterBudget()" in book_adapter.text
    assert "const parseFB2XML" in book_adapter.text
    assert "error.message !== 'FB2 is malformed XML.'" in book_adapter.text
    assert (
        "result.insertBefore(safe.createTextNode(' '), result.children[index])" in book_adapter.text
    )
    assert "canonicalizeFB2RasterImage" in book_adapter.text
    assert "candidateBinaries" in book_adapter.text
    assert "retainedBinaries" in book_adapter.text
    assert "if (!canonicalBodies.length)" in book_adapter.text
    assert "validateRasterImage(blob, item.mediaType, rasterBudget)" in book_adapter.text
    assert "canonicalizeFB2RasterImage" in policy.text
    assert "PNG_CANONICAL_CHUNKS" in policy.text
    assert "'tRNS'" in policy.text
    assert "fb2Nodes: 250_000" in policy.text
    assert "imageChunks: 16_384" in policy.text
    assert "canonicalBytes = bytes.slice(0, parsed.end)" in policy.text
    assert "if (!canonicalize) invalidRaster()" in policy.text
    assert "validateFB2Person" not in book_adapter.text
    assert foliate_fb2.text.count("for (const url of urls) URL.revokeObjectURL(url)") == 2
    assert "const converted = this.convert(item, STYLE)" in foliate_fb2.text
    assert "firstSection.insertBefore(content, firstSection.firstChild)" in foliate_fb2.text
    assert "mergedSectionTitles.set(firstSection" in foliate_fb2.text
    assert "convertedCover.classList.add('cover')" in foliate_fb2.text
    assert "frontMatter.push(converter.convert(annotation" in foliate_fb2.text
    assert ".filter(item => item.label)" in foliate_fb2.text
    assert "elements[0]?.localName === 'img'" in foliate_fb2.text
    assert "if (index === 0) mergeLeadingFrontMatter(converted)" in foliate_fb2.text

    for limit in (
        "imageDimension: 16_384",
        "imagePixels: 40_000_000",
        "imageFrames: 256",
        "publicationImagePixelFrames: 200_000_000",
        "publicationImageFrames: 1024",
    ):
        assert limit in policy.text
    for parser in ("parseJPEG", "parsePNG", "parseGIF", "parseWebP"):
        assert parser in policy.text
    assert "createImageBitmap" not in policy.text
    assert "source.arrayBuffer()" in policy.text
    assert "budget.pixelFrames += pixelFrames" in policy.text
    assert "animationFrames + (defaultImageIsSeparate ? 1 : 0)" in policy.text
    assert "frameControls !== animationFrames" in policy.text
    assert "type === 'iCCP' || type === 'zTXt'" in policy.text
    assert "bytes[keywordEnd + 1] === 1" in policy.text
    assert "marker === 0xdc" in policy.text
    assert "expectDNL = deferredWidth !== null" in policy.text

    title_at = paginator.text.index("this.#iframe.setAttribute('title', 'Book content')")
    attachment_at = paginator.text.index("this.#element.append(this.#iframe)")
    assert title_at < attachment_at


def test_reader_renders_known_size_limit() -> None:
    app, catalog, _ = _app()
    catalog.detail_size = 64 * 1024 * 1024 + 1
    with TestClient(app) as client:
        response = client.get(
            "/books/public-1/read",
            params={"include_hidden": "true"},
        )

    assert response.status_code == 200
    assert response.headers["content-security-policy"] == routes._READER_CSP
    assert "larger than the 64 MiB web reader limit" in response.text
    assert "Preparing the book for reading" in response.text
    assert '<link rel="stylesheet" href="/static/css/reader.css">' in response.text
    assert '<script defer src="/static/locale.js"></script>' in response.text
    assert "/static/reader/app.js" not in response.text
    assert re.search(r'data-reader-state="loading"[^>]* hidden', response.text)
    assert re.search(r'data-reader-state="error" role="alert">', response.text)
    assert _link_href(response.text, "reader-retry") == ("/books/public-1/read?include_hidden=true")
    assert _link_href(response.text, "reader-download") == "/books/public-1/download"
    assert _link_href(response.text, "reader-back") == ("/books/public-1?include_hidden=true")


def test_russian_reader_localizes_shell_switcher_payload_and_known_error() -> None:
    app, catalog, _ = _app()
    catalog.detail_title = 'Книга <unsafe> & "raw title"'
    headers = {"Accept-Language": "ru"}
    with TestClient(app) as client:
        reader = client.get("/books/public-1/read", headers=headers)
        catalog.detail_size = 64 * 1024 * 1024 + 1
        over_limit = client.get("/books/public-1/read", headers=headers)

    assert reader.status_code == over_limit.status_code == 200
    assert reader.headers["content-security-policy"] == routes._READER_CSP
    assert over_limit.headers["content-security-policy"] == routes._READER_CSP
    assert reader.headers["vary"] == "Cookie, Accept-Language"
    assert over_limit.headers["vary"] == "Cookie, Accept-Language"
    assert "set-cookie" not in reader.headers
    assert '<html lang="ru">' in reader.text
    assert 'data-ui-locale="ru"' in reader.text
    assert 'data-reader-locale="ru"' in reader.text
    assert 'aria-label="Язык интерфейса"' in reader.text
    assert 'data-locale-choice="ru" aria-pressed="true"' in reader.text
    assert "Подготовка книги к чтению…" in reader.text
    assert "<title>Книга &lt;unsafe&gt; &amp; &#34;raw title&#34; · SOPDS</title>" in reader.text
    assert "читалка SOPDS</title>" not in reader.text
    assert 'aria-label="Элементы управления чтением"' in reader.text
    assert 'data-reader-mode-toggle data-reader-mode="scroll"' in reader.text
    assert ">Страницы</span>" in reader.text
    assert "Книга &lt;unsafe&gt; &amp; &#34;raw title&#34;" in reader.text
    assert "Книга <unsafe>" not in reader.text

    messages_match = re.search(r'data-reader-messages="([^"]*)"', reader.text)
    assert messages_match is not None
    messages = json.loads(html.unescape(messages_match.group(1)))
    assert messages == {
        "pages": "Страницы",
        "scroll": "Прокрутка",
        "switchToPagesView": "Переключиться на постраничный режим",
        "switchToScrollView": "Переключиться на прокрутку",
        "previousPage": "Предыдущая страница",
        "nextPage": "Следующая страница",
        "genericOpenError": "Не удалось открыть книгу в веб-читалке.",  # noqa: RUF001
    }
    assert len(messages) == 7
    assert "Превышено ограничение веб-читалки в 64 MiB" in over_limit.text
    assert "Исходный файл всё ещё можно скачать." in over_limit.text
    assert ">Повторить</a>" in over_limit.text
    assert ">Скачать исходный файл</a>" in over_limit.text
    assert ">Назад к книге</a>" in over_limit.text
    assert '<script defer src="/static/locale.js"></script>' in over_limit.text
    assert "/static/reader/app.js" not in over_limit.text


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
    assert "Original file · 123 KB" in active.text
    assert ">FB2</a>" in active.text
    assert 'availability-badge--hidden">Hidden</span>' in hidden.text
    assert "Original file · 123 KB" in hidden.text
    assert ">FB2</a>" in hidden.text
    assert 'href="/?q=Series&amp;search_field=series&amp;include_hidden=true"' in hidden.text
    assert 'availability-badge--missed">Missed</span>' in missed.text
    assert "Original file unavailable" in missed.text
    assert "Download original" not in missed.text
    assert 'href="/books/public-1/download"' not in missed.text


def test_obsolete_exact_scope_params_are_ignored_without_scope_ui() -> None:
    app, catalog, _ = _app()
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
    assert 'name="author"' not in page.text
    assert 'name="series"' not in page.text
    assert "Searching within" not in page.text
    assert "scope-chip" not in page.text
    assert 'name="cursor"' not in page.text
    assert catalog.requests[-1] == CatalogRequest(query="book", page_size=1_000)
    assert (
        'class="catalog-clear" href="/" data-catalog-criteria-link '
        'aria-label="Clear search and filters">Clear all</a>' in page.text
    )


def test_long_author_lists_use_native_overflow_disclosure() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        page = client.get("/?q=many-authors")

    assert page.status_code == 200
    book = _catalog_payload(page.text)["books"][0]
    assert book["title"] == "Очень длинное название книги для проверки многоязычного каталога"
    assert [author["display"] for author in book["authors"]] == [
        "Тестов Тест",
        "Примеров Пример Примерович",
        "Third Author",
        "Fourth Author",
        "Fifth Author",
    ]
    assert "author-overflow" not in page.text
    assert "result-row__author-token" not in page.text


def test_catalog_metadata_groups_two_lines_without_dangling_separators() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        page = client.get("/?q=sparse-metadata")

    assert page.status_code == 200
    book = _catalog_payload(page.text)["books"][0]
    assert book["sourceFormat"] == {"key": "fb2", "label": "FB2"}
    assert book["size"] == 126_000
    assert book["sizeLabel"] == "123 KB"
    assert book["language"] is None
    assert book["publishedDate"] is None
    assert "result-metadata" not in page.text


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
    assert "data-catalog-sort-controls hidden" in catalog.text
    assert 'class="catalog-local-toolbar"' in catalog.text
    toolbar_start = catalog.text.index('class="catalog-local-toolbar"')
    assert toolbar_start < catalog.text.index('id="catalog-loaded-summary"')
    assert catalog.text.index('id="catalog-loaded-summary"') < catalog.text.index(
        'class="catalog-local-filters"', toolbar_start
    )
    assert ">Clear</button>" in catalog.text
    assert "Clear local filters" not in catalog.text
    assert "data-catalog-result-view" in catalog.text
    assert "data-catalog-payload" in catalog.text
    assert "result-row__body" not in catalog.text
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
    assert re.search(
        r"\.catalog-flat-view > \.result-row \{[^}]*17rem;",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.catalog-flat-view \.result-row__actions \{[^}]*flex-wrap: nowrap;",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.catalog-tree-view \.result-row__actions \{[^}]*flex-wrap: nowrap;",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.selected-table \.result-row__heading,\s*"
        r"\.catalog-table \.result-row__heading \{[^}]*display: flex;",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.selected-table \.result-row__title,\s*"
        r"\.catalog-table \.result-row__title \{[^}]*font-size: 0\.8rem;",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.catalog-tree-author > summary > input\.catalog-tree-select,[^{]*"
        r"\.catalog-tree-series > summary > input\.catalog-tree-select \{[^}]*"
        r"margin: 0 var\(--space-3\);",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.catalog-table th:has\(> \.catalog-table__sort\) \{[^}]*padding: 0;",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.catalog-table__sort \{[^}]*width: 100%;[^}]*justify-content: flex-start;",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.catalog-table td:not\(:last-child\) a \{[^}]*text-decoration: none;",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.catalog-table td:not\(:last-child\) a:hover,[^{]*"
        r"\.catalog-table td:not\(:last-child\) a:focus-visible \{[^}]*"
        r"text-decoration: underline;",
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


def test_expanded_tree_rows_use_hierarchical_backgrounds() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        stylesheet = client.get("/static/css/app.css")

    assert stylesheet.status_code == 200
    assert re.search(
        r"\.catalog-tree-author__summary \{[^}]*"
        r"background: rgb\(245 240 230 / 55%\);",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.catalog-tree-author\[open\] > \.catalog-tree-author__summary \{[^}]*"
        r"background: #e8f0eb;[^}]*color: var\(--color-forest-dark\);",
        stylesheet.text,
        re.S,
    )
    assert re.search(
        r"\.catalog-tree-series\[open\] > \.catalog-tree-series__summary \{[^}]*"
        r"background: var\(--color-paper-deep\);",
        stylesheet.text,
        re.S,
    )


def test_mobile_navigation_is_compact_and_keeps_secondary_links_in_overflow() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        page = client.get("/")
        stylesheet = client.get("/static/css/app.css")
        navigation = client.get("/static/navigation.js")

    assert page.status_code == stylesheet.status_code == navigation.status_code == 200
    assert 'data-more-navigation-label="More navigation"' in page.text
    assert '<aside class="app-sidebar">' in page.text
    assert '<nav class="site-navigation" data-more-navigation-label=' in page.text
    assert page.text.count('class="site-navigation__secondary"') == 0
    assert "/static/navigation.js" in page.text
    assert 'createMenu(\n    "mobile-language-menu"' in navigation.text
    assert '"mobile-secondary-navigation"' in navigation.text
    assert "secondaryLinks.forEach((link) => moreMenu.panel.append(link))" in navigation.text
    assert "closeMenus(menu)" in navigation.text
    assert 'event.key !== "Escape"' in navigation.text

    mobile_rules = stylesheet.text.split("@media (max-width: 48rem) {", 1)[1].split(
        "@media (max-width: 40rem)", 1
    )[0]
    assert re.search(
        r"\.app-sidebar \{[^}]*padding: var\(--space-1\) var\(--space-2\);",
        mobile_rules,
        re.S,
    )
    assert re.search(r"\.site-brand \{[^}]*align-self: center;", mobile_rules, re.S)
    assert re.search(r"\.site-navigation a \{[^}]*min-height: 2\.75rem;", mobile_rules, re.S)
    assert re.search(
        r"\.app-sidebar\[data-mobile-navigation-ready\]\s*"
        r"\.site-navigation a:nth-child\(n\+3\) \{[^}]*display: none;",
        mobile_rules,
        re.S,
    )
    assert re.search(r"\.mobile-navigation-actions \{[^}]*display: flex;", mobile_rules, re.S)
    assert ".mobile-navigation-menu[data-open] .mobile-navigation-menu__popover" in mobile_rules
    assert not re.search(r"\.site-navigation \{[^}]*display: grid;", mobile_rules, re.S)

    narrow_rules = stylesheet.text.split("@media (max-width: 34rem) {", 1)[1].split(
        "@media (pointer: coarse)", 1
    )[0]
    assert re.search(
        r"\.selected-tree-view \.result-row,\s*\.result-row \{[^}]*"
        r"grid-template-columns: 2rem minmax\(0, 1fr\);",
        narrow_rules,
        re.S,
    )


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
    assert "<title>Selected books · SOPDS</title>" in page.text
    assert '<a href="/selected" aria-current="page">Selected <span' in page.text
    assert "data-selection-count hidden>0</span>" in page.text
    assert "/static/book_sorting.js" in page.text
    assert "/static/selection.js" in page.text
    assert 'action="/selected/download"' in page.text
    assert 'method="post"' in page.text
    assert 'name="ids" value="[]" data-selected-ids' in page.text
    assert re.search(r'name="csrf_token" value="[A-Za-z0-9_-]+"', page.text)
    assert page.headers["cache-control"] == "no-store"
    assert "set-cookie" not in page.headers
    assert "data-selected-preview-target" in page.text
    assert 'data-selected-view="flat"' in page.text
    assert 'data-selected-view="tree"' in page.text
    assert 'data-selected-view="table"' in page.text
    assert "Uncheck books to exclude them from the archive." in page.text
    assert "Unchecked rows disappear" not in page.text
    assert '<option value="nested" selected>Author + series folders</option>' in page.text
    assert "Nested folders" not in page.text
    assert "data-selection-clear" in page.text
    assert "selected-toolbar__summary" not in page.text
    assert "data-selected-downloadable-count" not in page.text
    assert "data-selected-total-size" not in page.text
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
    assert "data-selection-remove" not in preview.text
    assert ">Remove<" not in preview.text
    assert 'data-selected-summary tabindex="-1"' in preview.text
    assert "Reader One/Selected Book.fb2" not in preview.text
    assert 'href="/books/public-1/read" target="_blank"' in preview.text
    assert 'href="/books/public-1/download"' in preview.text
    assert 'href="/books/public-1/download/epub"' in preview.text
    assert 'href="/books/public-1/download/azw3"' in preview.text
    assert 'href="/books/public-1">Details</a>' in preview.text
    assert ">Open details</a>" not in preview.text
    assert "return_to" not in preview.text
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
    assert archive.preview_requests[0].format == "original"
    assert archive.download_requests[0].format == "original"


def test_selected_format_requests_and_download_filenames_are_canonical() -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive

    with TestClient(app) as client:
        epub_preview = client.post(
            "/selected/preview",
            json={"ids": ["public-1"], "preset": "nested", "format": "epub"},
        )
        epub_download = client.post(
            "/selected/download",
            data=_download_form(app, '["public-1"]', "nested", "epub"),
        )
        azw3_download = client.post(
            "/selected/download",
            data=_download_form(app, '["public-1"]', "nested", "azw3"),
        )

    assert epub_preview.status_code == 200
    assert 'data-archive-format="epub"' in epub_preview.text
    assert "source size 321 bytes" in epub_preview.text
    assert 'filename="selected-books-epub.zip"' in epub_download.headers["content-disposition"]
    assert 'filename="selected-books-azw3.zip"' in azw3_download.headers["content-disposition"]
    assert archive.preview_requests == [ArchiveRequest(["public-1"], "nested", "epub")]
    assert archive.download_requests == [
        ArchiveRequest(["public-1"], "nested", "epub"),
        ArchiveRequest(["public-1"], "nested", "azw3"),
    ]


def test_catalog_selection_hooks_only_render_for_downloadable_non_missed_books() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        available = client.get("/?q=book")
        missed = client.get("/?q=missed&include_missed=true")
        unavailable = client.get("/?q=hidden-unavailable&include_hidden=true")
        management = client.get("/manage")

    available_book = _catalog_payload(available.text)["books"][0]
    missed_book = _catalog_payload(missed.text)["books"][0]
    unavailable_book = _catalog_payload(unavailable.text)["books"][0]
    assert available_book["selectable"] is True
    assert missed_book["selectable"] is False
    assert unavailable_book["selectable"] is False
    for response in (available, missed, unavailable):
        assert "data-selection-checkbox" not in response.text
    for response in (available, missed, unavailable, management):
        assert "<span data-selection-count hidden>0</span>" in response.text
        assert (
            '<script defer src="http://testserver/static/selection.js"></script>' in response.text
        )


def test_selected_preview_reuses_rows_and_marks_all_excluded_states_without_paths() -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive
    hidden = CatalogBook(
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
    missed = CatalogBook(
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
    assert "data-selection-remove" not in preview.text
    assert ">Remove<" not in preview.text
    assert 'data-status="downloadable" data-collision="true"' in preview.text
    assert 'data-status="unavailable" data-collision="false"' in preview.text
    assert 'data-status="unknown"' in preview.text
    assert "unavailable book is excluded" in preview.text
    assert "unknown selection is excluded" in preview.text
    assert "Archive name collisions affect 1 book" in preview.text
    assert "Archive name conflicts; ZIP names will be made unique." in preview.text
    assert 'href="/books/hidden-1/read?include_hidden=true" target="_blank"' in preview.text
    assert 'href="/books/hidden-1/download"' in preview.text
    assert 'href="/books/hidden-1/download/azw3"' in preview.text
    assert 'href="/books/missed-1/read' not in preview.text
    assert 'href="/books/missed-1/download' not in preview.text
    assert "Writer Hidden/Hidden Book.epub" not in preview.text
    assert "private/path/key" not in preview.text
    assert 'href="/books/hidden-1?include_hidden=true"' in preview.text
    assert 'data-title-sort-key="hidden book"' in preview.text
    assert "<span data-series-number>#2</span>" in preview.text
    assert 'href="/books/missed-1?include_missed=true"' in preview.text
    assert "return_to" not in preview.text


def test_selected_preview_distinguishes_unsupported_rows_and_capabilities() -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive
    unsupported = CatalogBook(
        public_id="azw3-1",
        title="Kindle Book",
        authors=("Writer,One,",),
        series=None,
        series_number=None,
        language="en",
        original_format="azw3",
        size=456,
    )
    request = ArchiveRequest([unsupported.public_id], "nested", "epub")
    archive.preview_value = ArchiveManifest(
        request,
        9,
        (
            ArchivePreviewEntry(
                unsupported.public_id,
                unsupported,
                ArchiveEntryStatus.UNSUPPORTED,
                supported_formats=("azw3",),
            ),
        ),
        (),
        0,
    )

    with TestClient(app) as client:
        preview = client.post(
            "/selected/preview",
            json={"ids": [unsupported.public_id], "preset": "nested", "format": "epub"},
        )

    assert preview.status_code == 200
    assert 'data-status="unsupported"' in preview.text
    assert 'data-source-downloadable="true"' in preview.text
    assert 'data-source-format="AZW3"' in preview.text
    assert 'data-output-formats="azw3"' in preview.text
    assert "1 book cannot produce EPUB and is excluded" in preview.text
    assert "cannot produce the selected ZIP format" in preview.text
    assert "Unsupported</span>" in preview.text
    assert 'href="/books/azw3-1/download"' in preview.text
    assert 'href="/books/azw3-1/read' not in preview.text
    assert "data-selection-remove" not in preview.text
    assert "unavailable" not in preview.text.casefold()


def test_selected_preview_omits_read_for_oversized_supported_books() -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive
    oversized = CatalogBook(
        public_id="large-1",
        title="Large Book",
        authors=("Writer,Large,",),
        series=None,
        series_number=None,
        language="en",
        original_format="fb2",
        size=(64 * 1024 * 1024) + 1,
    )
    request = ArchiveRequest([oversized.public_id], "nested")
    member = ArchiveMember(
        oversized.public_id,
        oversized,
        "Writer Large/Large Book.fb2",
        "Writer Large/Large Book.fb2",
    )
    archive.preview_value = ArchiveManifest(
        request,
        9,
        (
            ArchivePreviewEntry(
                oversized.public_id,
                oversized,
                ArchiveEntryStatus.DOWNLOADABLE,
            ),
        ),
        (member,),
        oversized.size,
    )

    with TestClient(app) as client:
        preview = client.post(
            "/selected/preview",
            json={"ids": [oversized.public_id], "preset": "nested"},
        )

    assert preview.status_code == 200
    assert 'href="/books/large-1/download"' in preview.text
    assert 'href="/books/large-1/download/epub"' in preview.text
    assert 'href="/books/large-1/download/azw3"' in preview.text
    assert 'href="/books/large-1/read' not in preview.text


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
        "mutateSelection(() => [], true, true)",
        "resetPreviewState(page)",
        "restorePreviewFocus(target, requestIds)",
        'target.querySelector("[data-selected-preview-error]")',
        "showPreviewError(target)",
        "mergeSelectedPreview(target, incomingContent)",
        "showPreservedPreviewError(target, incomingContent)",
        "createSelectedEmptyState()",
        '"result-list selected-result-list selected-flat-view catalog-flat-view"',
        "const includedIds = new Set(selectedIds)",
        "hasExcludedDisplayedEntries()",
        "refreshSelectedPreview({preserveEntries})",
        'response.ok && !incomingContent.hasAttribute("data-selected-preview-error")',
        "syncFormatSelector(page)",
        "hasAuthoritativePreviewRows(page)",
        "authoritativePreviewIds = new Set(requestIds)",
        "authoritativePreviewIds.has(publicId) && displayedIds.has(publicId)",
        "updateSelectedEntry(entry, incoming)",
        'data-included="true"][data-source-downloadable="true"]',
        'sourceFormats.size === 1 ? [...sourceFormats][0] : selectionMessage("original")',
        "availableTargets.has(value)",
        'selector.value = "original"',
        "format: selectedFormat.value",
        'selectedFormat.addEventListener("change"',
        'selectionMessage(content.dataset.archiveFormat === "original" ? "size" : "sourceSize")',
        'window.fetch("/selected/preview"',
        '"Content-Type": "application/json"',
        "response.text()",
    ):
        assert contract in script.text
    loading_markup = 'target.replaceChildren(selectedElement("p", "selected-loading", selectionMessage("loadingSelection")));'
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
    assert "count.textContent = formatInteger(selectedIds.length);" in script.text
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
    assert 'error.dataset.selectedPreviewError = "";' in script.text
    assert 'error.setAttribute("role", "alert");' in script.text
    assert 'error.setAttribute("tabindex", "-1");' in script.text
    assert 'selectionMessage("couldNotLoadPreview")' in script.text
    assert "querySelector(`[data-public-id=" not in script.text
    assert "Blob" not in script.text
    assert 'document.addEventListener("htmx:responseError"' in csrf_script.text
    assert "xhr.status !== 403" in csrf_script.text
    assert 'xhr.getResponseHeader("X-SOPDS-CSRF-Expired")' in csrf_script.text
    assert "target.innerHTML = xhr.responseText" in csrf_script.text
    assert 'method="post" action="/selected/download"' in page.text
    assert 'type="hidden" name="ids"' in page.text
    assert 'type="hidden" name="csrf_token"' in page.text
    assert 'id="selected-format" name="format" data-selected-format' in page.text
    assert '<option value="original" selected>Original</option>' in page.text
    assert '<option value="epub"' not in page.text
    assert '<option value="azw3"' not in page.text
    assert '<option value="nested" selected>' in page.text
    assert '<option value="flatten">' in page.text
    assert '<option value="list">' in page.text


@pytest.mark.parametrize(
    ("body", "status_code"),
    [
        (b'[{"ids": [], "preset": "nested"}]', 422),
        (b'{"ids": [], "preset": "nested", "extra": true}', 422),
        (b'{"ids": [], "preset": "nested", "format": "EPUB"}', 422),
        (b'{"ids": []}', 422),
        (b'{"ids": [], "ids": [], "preset": "nested"}', 400),
        (b'{"ids": [], "preset": "nested", "format": "epub", "format": "azw3"}', 400),
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
        or "Invalid archive format" in response.text
        or "Invalid selected-books request" in response.text
    )
    assert len(response.content) < 1_000
    assert app.state.archive.preview_requests == []


@pytest.mark.parametrize(
    "body",
    [
        "ids=%5B%22public-1%22%5D&preset=nested&extra=x",
        "ids=%5B%22public-1%22%5D&preset=nested&format=epub&format=azw3",
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
    assert len(response.content) < 5_000
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
    assert len(response.content) < 5_000
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
    assert len(response.content) < 5_000
    assert "/private/" not in response.text
    assert "secret-public-id" not in response.text
    assert "/private/" not in caplog.text
    assert "secret-public-id" not in caplog.text


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (ArchiveLimitError("Selected books exceed the source-size limit"), 413),
        (CatalogInputError("catalog detail that must not be reflected"), 422),
        (RuntimeError("/private/catalog-database"), 500),
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


async def test_discard_route_task_disposes_result_while_owner_is_cancelling() -> None:
    result = object()

    async def completed() -> object:
        return result

    operation = asyncio.create_task(completed())
    await operation
    disposed: list[object] = []

    async def dispose(value: object) -> bool:
        disposed.append(value)
        return False

    owner = asyncio.current_task()
    assert owner is not None
    owner.cancel()
    try:
        cancelled = await routes._discard_route_task(
            operation,
            cancel=False,
            dispose=dispose,
        )
    finally:
        owner.uncancel()

    assert cancelled
    assert disposed == [result]


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


def test_book_detail_selected_return_context_is_client_owned() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        selected = client.get("/books/public-1", params={"return_to": "/selected"})

    assert _link_href(selected.text, "detail-back-link") == "/"
    assert "data-detail-back" in selected.text
    assert "Back to results" in selected.text
    assert "return_to" not in selected.text


@pytest.mark.parametrize(
    ("source_format", "targets"),
    [
        ("fb2", ("epub", "azw3")),
        ("epub", ("azw3",)),
        ("azw3", ()),
        ("pdf", ()),
    ],
)
def test_download_controls_show_only_actual_non_duplicate_conversions(
    source_format: str, targets: tuple[str, ...]
) -> None:
    app, catalog, _ = _app()
    catalog.original_format = source_format

    with TestClient(app) as client:
        page = client.get("/?q=book")
        detail = client.get("/books/public-1")

    label = source_format.upper()
    book = _catalog_payload(page.text)["books"][0]
    assert book["originalDownload"] == {
        "url": "/books/public-1/download",
        "label": label,
    }
    assert [conversion["url"].rsplit("/", 1)[-1] for conversion in book["conversions"]] == [
        target for target in ("epub", "azw3") if target in targets
    ]
    assert f'aria-label="Download original {label} file for A Book">{label}</a>' in detail.text
    for target in ("epub", "azw3"):
        path = f"/books/public-1/download/{target}"
        assert (path in detail.text) is (target in targets)
    assert ('<details class="download-menu download-menu--detail">' in detail.text) is bool(targets)


def test_converted_download_headers_body_and_bounded_target() -> None:
    app, _, _ = _app()
    conversion: _Conversion = app.state.conversion

    with TestClient(app) as client:
        response = client.get("/books/public-1/download/epub")
        invalid = client.get("/books/public-1/download/mobi")

    assert response.status_code == 200
    assert response.content == b"converted"
    assert response.headers["content-type"] == "application/epub+zip"
    assert response.headers["content-length"] == "9"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert conversion.last_stream is not None and conversion.last_stream.closed
    assert conversion.calls == [("public-1", "epub")]
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store"
    assert "mobi" not in invalid.text


@pytest.mark.parametrize(
    ("error", "status_code"),
    [
        (UnsupportedConversionError("/private/source.fb2"), 422),
        (SourceUnavailableError("/private/source.fb2"), 404),
        (ConversionSourceError("/private/source.fb2"), 500),
        (ConversionTimeoutError("/private/source.fb2"), 504),
        (SourceChangedError("/private/source.fb2"), 409),
        (ConverterExecutionError("/private/source.fb2"), 500),
        (InvalidConversionOutputError("/private/source.fb2"), 500),
        (ConversionShutdownError("/private/source.fb2"), 503),
    ],
)
def test_converted_download_error_mappings_are_path_free(
    error: Exception,
    status_code: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    app, _, _ = _app()
    conversion: _Conversion = app.state.conversion
    conversion.error = error

    with TestClient(app) as client:
        response = client.get("/books/secret-public-id/download/epub")

    assert response.status_code == status_code
    assert response.headers["cache-control"] == "no-store"
    assert "/private/" not in response.text
    assert "secret-public-id" not in response.text
    assert "/private/" not in caplog.text
    assert "secret-public-id" not in caplog.text


async def test_converted_download_disconnect_cancels_and_drains_conversion() -> None:
    app, _, _ = _app()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleaned = asyncio.Event()

    class BlockingConversion:
        async def convert(self, _public_id: str, _target_format: str) -> ConversionResult:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cleaned.set()
            raise AssertionError("cancelled conversion resumed")

    app.state.conversion = BlockingConversion()
    disconnected = asyncio.Event()
    messages: list[Message] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/books/public-1/download/epub",
        "raw_path": b"/books/public-1/download/epub",
        "query_string": b"",
        "headers": [(b"host", b"catalog.example")],
        "client": ("127.0.0.1", 1234),
        "server": ("catalog.example", 443),
    }

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    serving = asyncio.create_task(app(scope, receive, send))
    await started.wait()
    disconnected.set()
    await cleanup_started.wait()
    await asyncio.sleep(0)
    assert not serving.done()
    assert messages == []

    cleanup_release.set()
    await serving

    assert cleaned.is_set()
    assert messages == []


async def test_converted_download_disconnect_cancels_real_single_flight_producer(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "converter-ready"
    terminated = tmp_path / "converter-terminated"
    executable = tmp_path / "gated-fbc"
    executable.write_text(
        """#!/usr/bin/env python3
import signal
import sys
import time
from pathlib import Path

ready = Path("""
        + repr(os.fspath(ready))
        + """)
terminated = Path("""
        + repr(os.fspath(terminated))
        + """)
output = Path(sys.argv[sys.argv.index("--output-file") + 1])
output.write_bytes(b"partial")
ready.write_text(str(__import__("os").getpid()))

def stop(*_args):
    terminated.write_text("terminated")
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
time.sleep(30)
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)

    class ConversionAcquisition:
        def __init__(self) -> None:
            self.streams: list[_Stream] = []

        async def describe(
            self,
            public_id: str,
            *,
            expected_generation_id: int | None = None,
        ) -> OriginalDescription:
            del expected_generation_id
            return OriginalDescription(public_id, "A Book", "fb2", 8, _REVISION)

        async def acquire(
            self,
            public_id: str,
            *,
            expected_generation_id: int | None = None,
        ) -> AcquiredOriginal:
            del public_id, expected_generation_id
            stream = _Stream(b"original")
            self.streams.append(stream)
            return AcquiredOriginal(
                "book.fb2",
                "application/x-fictionbook+xml",
                8,
                stream,
                "fb2",
                _REVISION,
            )

    app, _, _ = _app()
    acquisition = ConversionAcquisition()
    converter = Fb2ToEpubConverter(executable=os.fspath(executable))
    registry = ConverterRegistry((converter,))
    cache_dir = tmp_path / "cache"
    cache = ArtifactCache(cache_dir, 60)
    await cache.startup()
    service = ConversionService(acquisition, registry, cache)
    app.state.acquisition = acquisition
    app.state.converter_registry = registry
    app.state.conversion = service
    disconnected = asyncio.Event()
    messages: list[Message] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/books/public-1/download/epub",
        "raw_path": b"/books/public-1/download/epub",
        "query_string": b"",
        "headers": [(b"host", b"catalog.example")],
        "client": ("127.0.0.1", 1234),
        "server": ("catalog.example", 443),
    }

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    try:
        serving = asyncio.create_task(app(scope, receive, send))
        for _ in range(200):
            if ready.exists():
                break
            if serving.done():
                raise AssertionError("conversion route completed before converter started")
            await asyncio.sleep(0.01)
        assert ready.exists()
        process_id = int(ready.read_text())
        disconnected.set()
        await asyncio.wait_for(serving, 3)

        assert terminated.read_text() == "terminated"
        with pytest.raises(ProcessLookupError):
            os.kill(process_id, 0)
        assert acquisition.streams[0].closed
        assert not list(cache_dir.glob("*.source"))
        assert not list(cache_dir.glob("*.tmp"))
        assert not list(cache_dir.glob("*.artifact"))
        assert messages == []
    finally:
        await service.shutdown()


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
    assert re.fullmatch(r"[0-9a-f]{64}", response.headers["x-sopds-source-revision"])
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


def test_original_download_revision_header_is_stable_and_changes_with_acquired_source() -> None:
    app, _, _ = _app()
    acquisition: _Acquisition = app.state.acquisition
    with TestClient(app) as client:
        first = client.get("/books/public-1/download")
        acquisition.stream = _Stream()
        second = client.get("/books/public-1/download")
        acquisition.revision = SourceRevision(1, 2, 4)
        acquisition.stream = _Stream()
        changed = client.get("/books/public-1/download")

    first_token = first.headers["x-sopds-source-revision"]
    assert first.status_code == second.status_code == changed.status_code == 200
    assert first_token == second.headers["x-sopds-source-revision"]
    assert changed.headers["x-sopds-source-revision"] != first_token
    assert re.fullmatch(r"[0-9a-f]{64}", changed.headers["x-sopds-source-revision"])
    assert "1:2:" not in first_token


async def test_reader_source_fetch_disconnect_closes_acquired_stream() -> None:
    started = asyncio.Event()
    disconnected = asyncio.Event()

    class BlockingStream(_Stream):
        @override
        async def _iterate(self) -> AsyncIterator[bytes]:
            started.set()
            await asyncio.Event().wait()
            yield b"unreachable"

    class BlockingAcquisition:
        def __init__(self) -> None:
            self.stream = BlockingStream()

        async def acquire(self, public_id: str) -> AcquiredOriginal:
            del public_id
            return AcquiredOriginal(
                "book.fb2",
                "application/x-fictionbook+xml",
                8,
                self.stream,
                "fb2",
                _REVISION,
            )

    app, _, _ = _app()
    acquisition = BlockingAcquisition()
    app.state.acquisition = acquisition
    messages: list[Message] = []
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "https",
        "path": "/books/public-1/download",
        "raw_path": b"/books/public-1/download",
        "query_string": b"",
        "headers": [(b"host", b"catalog.example")],
        "client": ("127.0.0.1", 1234),
        "server": ("catalog.example", 443),
    }

    async def receive() -> Message:
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)

    serving = asyncio.create_task(app(scope, receive, send))
    await started.wait()
    disconnected.set()
    await asyncio.wait_for(serving, 1)

    assert acquisition.stream.closed
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    headers = dict(response_start["headers"])
    assert re.fullmatch(rb"[0-9a-f]{64}", headers[b"x-sopds-source-revision"])


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


def test_russian_server_rendered_surfaces_preserve_catalog_data() -> None:
    imports = _Imports(_status(ImportState.FAILED, error_summary="raw parser failure"))
    app, catalog, _ = _app(imports)
    catalog.detail_published_date = date(2024, 2, 3)
    catalog.detail_keywords = "raw, metadata"

    with TestClient(app) as client:
        headers = {"Accept-Language": "ru-RU, en"}
        shell = client.get("/", headers=headers)
        results = client.get("/?q=book", headers=headers)
        fragment = client.get(
            "/catalog-fragment?q=none",
            headers={**headers, "HX-Request": "true"},
        )
        detail = client.get("/books/public-1", headers=headers)
        selected_page = client.get("/selected", headers=headers)
        selected_error = client.post(
            "/selected/preview",
            content=b"not-json",
            headers={**headers, "Content-Type": "application/json"},
        )
        management = client.get("/manage", headers=headers)

    assert '<html lang="ru">' in shell.text
    assert "<title>Каталог · SOPDS</title>" in shell.text
    assert "Большая библиотека — простой поиск" in shell.text
    assert "<title>A Book · SOPDS</title>" in detail.text
    assert "<title>Выбранные книги · SOPDS</title>" in selected_page.text
    assert "<title>Управление каталогом · SOPDS</title>" in management.text
    assert ">Поиск по каталогу</h1>" in shell.text
    assert 'aria-label="Язык интерфейса"' in shell.text
    assert 'data-locale-choice="ru" aria-pressed="true"' in shell.text
    assert "Загружено: 1 · Есть другие совпадения" in results.text
    assert "Книги не найдены" in fragment.text
    assert "Сведения о книге" in detail.text  # noqa: RUF001
    for raw_value in ("A Book", "Тестов Тест", "Science fiction", "2024-02-03", "123 KB"):
        assert raw_value in detail.text
    assert "Выбранные книги" in selected_page.text
    assert "Структура ZIP" in selected_page.text
    assert "Некорректный запрос выбранных книг" in selected_error.text
    assert "Управление каталогом" in management.text
    assert "Ошибка" in management.text
    assert "Вручную" in management.text
    assert "raw parser failure" in management.text


def test_russian_read_contexts_distinguish_action_from_import_metric() -> None:
    imports = _Imports(_status(ImportState.SUCCEEDED, records_read=702_461))
    app, _, _ = _app(imports)

    with TestClient(app) as client:
        headers = {"Accept-Language": "ru"}
        detail = client.get("/books/public-1", headers=headers)
        import_status = client.get("/imports/status", headers=headers)

    assert 'rel="noopener noreferrer">Читать</a>' in detail.text
    assert "<dt>Прочитано</dt><dd>702 461</dd>" in import_status.text
    assert "<dt>Читать</dt>" not in import_status.text


@pytest.mark.parametrize(
    ("count", "wording"),
    [
        (1, "1 книга не может быть преобразована"),
        (2, "2 книги не могут быть преобразованы"),
        (5, "5 книг не могут быть преобразованы"),
        (11, "11 книг не могут быть преобразованы"),
        (21, "21 книга не может быть преобразована"),
    ],
)
def test_russian_selected_preview_uses_server_plural_forms(count: int, wording: str) -> None:
    app, _, _ = _app()
    archive: _Archive = app.state.archive
    ids = [f"book-{index}" for index in range(count)]
    request = ArchiveRequest(ids, "nested", "epub")
    entries = tuple(
        ArchivePreviewEntry(
            public_id,
            CatalogBook(
                public_id=public_id,
                title=f"Book {index}",
                authors=("Author,One,",),
                series=None,
                series_number=None,
                language="en",
                original_format="azw3",
                size=1,
            ),
            ArchiveEntryStatus.UNSUPPORTED,
        )
        for index, public_id in enumerate(ids)
    )
    archive.preview_value = ArchiveManifest(request, 7, entries, (), 0)

    with TestClient(app) as client:
        response = client.post(
            "/selected/preview",
            json={"ids": ids, "preset": "nested", "format": "epub"},
            headers={"Accept-Language": "ru"},
        )

    assert response.status_code == 200
    assert wording in response.text
    assert "EPUB" in response.text


def test_localized_html_advertises_locale_and_varies_without_setting_a_cookie() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        responses = (
            client.get("/", headers={"Accept-Language": "ru"}),
            client.get("/catalog-fragment?q=book", headers={"Accept-Language": "ru"}),
            client.get("/books/public-1", headers={"Cookie": "sopds_ui_language=ru"}),
            client.get("/books/public-1/read", headers={"Accept-Language": "ru"}),
            client.get("/selected", headers={"Accept-Language": "ru"}),
            client.get("/manage", headers={"Accept-Language": "ru"}),
            client.get("/imports/status", headers={"Accept-Language": "ru"}),
            client.post("/imports", headers={"Accept-Language": "ru"}),
        )

    for response in responses:
        assert response.headers["content-language"] == "ru"
        assert response.headers["vary"] == "Cookie, Accept-Language"
        assert "set-cookie" not in response.headers
    assert responses[4].headers["cache-control"] == "no-store"
    assert responses[5].headers["cache-control"] == "no-store"
    assert responses[7].headers["x-sopds-csrf-expired"] == "true"


def test_cookie_locale_precedes_header_across_full_pages_and_fragments() -> None:
    app, _, _ = _app()
    with TestClient(app) as client:
        russian = client.get(
            "/", headers={"Cookie": "sopds_ui_language=ru", "Accept-Language": "en"}
        )
        english = client.get(
            "/catalog-fragment?q=none",
            headers={"Cookie": "sopds_ui_language=en", "Accept-Language": "ru"},
        )

    assert '<html lang="ru">' in russian.text
    assert "Поиск по каталогу" in russian.text
    assert "No books found" in english.text
    assert "Книги не найдены" not in english.text
    assert russian.headers["content-language"] == "ru"
    assert english.headers["content-language"] == "en"


def test_russian_catalog_errors_are_allowlisted_and_unknown_details_are_hidden() -> None:
    app, catalog, _ = _app()

    async def unknown_browse(request: CatalogRequest) -> CatalogPage:
        catalog.requests.append(request)
        raise CatalogInputError("/private/catalog diagnostic")

    catalog.browse = unknown_browse  # type: ignore[method-assign]
    with TestClient(app) as client:
        response = client.get("/?q=book", headers={"Accept-Language": "ru"})
        fragment = client.get(
            "/catalog-fragment?q=book",
            headers={"Accept-Language": "ru", "HX-Request": "true"},
        )

    assert response.status_code == 400
    assert "Не удалось выполнить запрос к каталогу" in response.text  # noqa: RUF001
    assert fragment.status_code == 200
    assert "Не удалось выполнить запрос к каталогу" in fragment.text  # noqa: RUF001
    assert "/private/catalog diagnostic" not in response.text + fragment.text
    assert fragment.headers["HX-Push-Url"].startswith("/?q=book")
    assert fragment.headers["vary"] == "Cookie, Accept-Language"


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
