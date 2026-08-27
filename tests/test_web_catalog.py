"""Web adapter tests for catalog rendering, status polling, and manual import CSRF."""

import asyncio
import secrets
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import override

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
    BookDetail,
    BookSummary,
    CatalogFilters,
    CatalogPage,
    CatalogRequest,
    CatalogStatistics,
    FilterOption,
)
from sopds.imports.status import ImportState, ImportStatus, ImportTrigger
from sopds.web import routes

_REVISION = SourceRevision(1, 2, 3)


class _Catalog:
    def __init__(self) -> None:
        self.requests: list[CatalogRequest] = []

    async def browse(self, request: CatalogRequest) -> CatalogPage:
        self.requests.append(request)
        if request.query == "none":
            return CatalogPage((), None)
        return CatalogPage(
            books=(
                BookSummary(
                    public_id="public-1",
                    title="A Book",
                    authors=("Author",),
                    series="Series",
                    series_number="1",
                    language="en",
                    original_format="fb2",
                ),
            ),
            next_cursor="next-token" if request.cursor is None else None,
        )

    async def details(self, public_id: str) -> BookDetail | None:
        if public_id != "public-1":
            return None
        return BookDetail(
            public_id=public_id,
            title="A Book",
            authors=("Author",),
            genres=(("sf", "Science fiction"),),
            series="Series",
            series_number="1",
            size=123,
            libid=None,
            published_date=None,
            language="en",
            original_format="fb2",
            rating=None,
            keywords=None,
        )

    async def filters(self) -> CatalogFilters:
        return CatalogFilters(
            languages=(FilterOption("en", "en"),),
            genres=(FilterOption("sf", "Science fiction"),),
            original_formats=(FilterOption("fb2", "fb2"),),
        )

    async def statistics(self) -> CatalogStatistics:
        return CatalogStatistics(
            total_books=20,
            hidden_books=3,
            missed_books=5,
            active_books=12,
            generation_activated_at=datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC),
            database_size_bytes=2 * 1024 * 1024,
        )


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
        self.accept = True
        self.started: list[bool] = []
        self.vacuumed = True
        self.vacuum_calls = 0

    async def get_status(self) -> ImportStatus | None:
        return self.status

    def is_import_active(self) -> bool:
        return self.active

    def start_manual_import(self, *, force: bool = False) -> bool:
        self.started.append(force)
        return self.accept

    async def vacuum_database(self) -> bool:
        self.vacuum_calls += 1
        return self.vacuumed


def _status(state: ImportState, run_id: int = 1) -> ImportStatus:
    return ImportStatus(
        run_id=run_id,
        trigger=ImportTrigger.MANUAL,
        state=state,
        started_at=datetime(2025, 1, 1, tzinfo=UTC),
        finished_at=None if state is ImportState.RUNNING else datetime(2025, 1, 2, tzinfo=UTC),
        attempted_fingerprint=None,
        records_read=3,
        records_imported=2,
        records_deleted=1,
        records_rejected=0,
        error_summary=None,
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
        page = client.get("/?q=book&language=en&genre=sf&original_format=fb2")
        fragment = client.get("/catalog-fragment?q=book&language=en&genre=sf")
        full_next = client.get(
            "/?q=book&language=en&genre=sf&original_format=fb2&cursor=next-token"
        )
        detail = client.get("/books/public-1")
        missing = client.get("/books/missing")

    assert page.status_code == 200
    assert "A Book" in page.text
    assert "Science fiction" in page.text
    assert "/static/vendor/htmx/htmx-2.0.10.min.js" in page.text
    assert page.text.index('id="health"') < page.text.index("<main>")
    assert (
        '<link rel="alternate" '
        'type="application/atom+xml;profile=opds-catalog;kind=navigation" '
        'href="https://catalog.example/root/opds/">'
    ) in page.text
    assert "next-token" in page.text
    assert "Total books</dt><dd>20" in page.text
    assert "Hidden books</dt><dd>3" in page.text
    assert "Missed books</dt><dd>5" in page.text
    assert "Active books</dt><dd>12" in page.text
    assert "2.0 MiB" in page.text
    assert 'datetime="2025-01-02T03:04:05+00:00"' in page.text
    assert 'hx-post="/imports"' in page.text
    assert 'hx-post="/imports/force"' in page.text
    assert 'hx-post="/database/vacuum"' in page.text
    assert page.text.count('hx-target="#operation-status"') == 3
    assert page.text.index('id="operation-status"') > page.text.index('hx-post="/database/vacuum"')
    assert (
        'href="/?q=book&amp;language=en&amp;genre=sf&amp;original_format=fb2&amp;cursor=next-token"'
        in page.text
    )
    assert (
        'hx-get="/catalog-fragment?q=book&amp;language=en&amp;genre=sf&amp;original_format=fb2&amp;cursor=next-token"'
        in page.text
    )
    assert 'hx-push-url="true"' not in page.text
    assert fragment.status_code == 200
    assert fragment.headers["HX-Push-Url"] == (
        "/?q=book&language=en&genre=sf&original_format=&cursor="
    )
    assert "/catalog-fragment" not in fragment.headers["HX-Push-Url"]
    assert "<html" not in fragment.text
    assert full_next.status_code == 200
    assert (
        CatalogRequest(
            query="book",
            language="en",
            genre="sf",
            original_format="fb2",
            cursor="next-token",
        )
        in catalog.requests
    )
    assert detail.status_code == 200
    assert "Original format" in detail.text
    assert 'href="/books/public-1/download"' in detail.text
    assert missing.status_code == 404
    assert catalog.requests[0] == CatalogRequest(
        query="book", language="en", genre="sf", original_format="fb2"
    )


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


def test_main_page_polls_while_active_import_has_no_persisted_status() -> None:
    imports = _Imports(active=True)
    app, _, _ = _app(imports)
    with TestClient(app) as client:
        page = client.get("/")
        pending = client.get("/imports/status")
        imports.status = _status(ImportState.RUNNING)
        started = client.get("/imports/status")

    assert "No import has run yet" not in page.text
    assert "Catalog import is starting" in page.text
    assert "Waiting for the import run" in page.text
    assert 'hx-get="/imports/status"' in page.text
    assert "Catalog import is starting" in pending.text
    assert 'hx-get="/imports/status"' in pending.text
    assert "2 imported" in started.text


def test_import_status_polls_only_while_running() -> None:
    imports = _Imports(_status(ImportState.RUNNING))
    app, _, _ = _app(imports)
    with TestClient(app) as client:
        running = client.get("/imports/status")
        imports.status = _status(ImportState.SUCCEEDED)
        terminal = client.get("/imports/status")

    assert 'hx-get="/imports/status"' in running.text
    assert 'hx-trigger="every 2s"' in running.text
    assert "2 imported" in running.text
    assert 'hx-get="/imports/status"' not in terminal.text
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
    assert "2 imported" not in accepted.text
    assert "after_run_id=1" in accepted.text
    assert "Waiting for the import run" in pending.text
    assert "2 imported" in started.text
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
    assert vacuumed.headers["HX-Trigger"] == "catalogChanged"
    assert "Database size" not in vacuumed.text
    assert busy.status_code == 200
    assert "VACUUM skipped" in busy.text
    assert "HX-Trigger" not in busy.headers
    assert imports.vacuum_calls == 2
