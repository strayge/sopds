"""Web adapter tests for catalog rendering, status polling, and manual import CSRF."""

import secrets
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from sopds.catalog.contracts import (
    BookDetail,
    BookSummary,
    CatalogFilters,
    CatalogPage,
    CatalogRequest,
    FilterOption,
)
from sopds.imports.status import ImportState, ImportStatus, ImportTrigger
from sopds.web import routes


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


class _Imports:
    def __init__(self, status: ImportStatus | None = None) -> None:
        self.status = status
        self.accept = True
        self.started = 0

    async def get_status(self) -> ImportStatus | None:
        return self.status

    def start_manual_import(self) -> bool:
        self.started += 1
        return self.accept


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
    app.state.import_coordinator = import_provider
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
    assert "next-token" in page.text
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
    assert missing.status_code == 404
    assert catalog.requests[0] == CatalogRequest(
        query="book", language="en", genre="sf", original_format="fb2"
    )


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


def test_manual_import_requires_csrf_and_reports_acceptance_or_conflict() -> None:
    imports = _Imports(_status(ImportState.RUNNING))
    app, _, _ = _app(imports)
    csrf_token = app.state.csrf_token
    with TestClient(app) as client:
        missing = client.post("/imports")
        invalid = client.post("/imports", headers={"X-CSRF-Token": "wrong"})
        accepted = client.post("/imports", headers={"X-CSRF-Token": csrf_token})
        pending = client.get("/imports/status?after_run_id=1")
        imports.status = _status(ImportState.RUNNING, run_id=2)
        started = client.get("/imports/status?after_run_id=1")
        imports.accept = False
        conflict = client.post("/imports", headers={"X-CSRF-Token": csrf_token})

    assert missing.status_code == 403
    assert invalid.status_code == 403
    assert accepted.status_code == 202
    assert "Manual import is starting" in accepted.text
    assert "2 imported" not in accepted.text
    assert "after_run_id=1" in accepted.text
    assert "Waiting for the import run" in pending.text
    assert "2 imported" in started.text
    assert conflict.status_code == 409
    assert "already running" in conflict.text
    assert imports.started == 2
