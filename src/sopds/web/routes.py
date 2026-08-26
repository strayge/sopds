"""Server-rendered catalog and operational status routes."""

import secrets
from pathlib import Path
from typing import Literal, cast
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from sopds.catalog.contracts import Catalog, CatalogInputError, CatalogRequest
from sopds.imports.status import ImportState, ImportStatus, ImportStatusProvider

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


def _catalog(request: Request) -> Catalog:
    return cast(Catalog, request.app.state.catalog)


def _imports(request: Request) -> ImportStatusProvider:
    return cast(ImportStatusProvider, request.app.state.import_coordinator)


def _catalog_request(
    q: str,
    language: str | None,
    genre: str | None,
    original_format: str | None,
    cursor: str | None,
) -> CatalogRequest:
    return CatalogRequest(
        query=q,
        language=language or None,
        genre=genre or None,
        original_format=original_format or None,
        cursor=cursor or None,
    )


def _catalog_url(path: str, catalog_request: CatalogRequest, cursor: str | None) -> str:
    values = {
        "q": catalog_request.query,
        "language": catalog_request.language or "",
        "genre": catalog_request.genre or "",
        "original_format": catalog_request.original_format or "",
        "cursor": cursor or "",
    }
    return f"{path}?{urlencode(values)}"


def _next_urls(
    catalog_request: CatalogRequest, next_cursor: str | None
) -> tuple[str | None, str | None]:
    if next_cursor is None:
        return None, None
    return (
        _catalog_url("/", catalog_request, next_cursor),
        _catalog_url("/catalog-fragment", catalog_request, next_cursor),
    )


async def _results_context(request: Request, catalog_request: CatalogRequest) -> dict[str, object]:
    page = await _catalog(request).browse(catalog_request)
    next_href, next_hx_url = _next_urls(catalog_request, page.next_cursor)
    return {
        "request": request,
        "page": page,
        "catalog_request": catalog_request,
        "next_href": next_href,
        "next_hx_url": next_hx_url,
    }


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    language: str | None = None,
    genre: str | None = None,
    original_format: str | None = None,
    cursor: str | None = None,
) -> Response:
    catalog_request = _catalog_request(q, language, genre, original_format, cursor)
    try:
        context = await _results_context(request, catalog_request)
        context.update(
            filters=await _catalog(request).filters(),
            import_status=await _imports(request).get_status(),
            csrf_token=cast(str, request.app.state.csrf_token),
            ImportState=ImportState,
            message=None,
            poll=False,
            pending=False,
            poll_after_run_id=None,
        )
        return templates.TemplateResponse(request=request, name="index.html", context=context)
    except CatalogInputError as error:
        return templates.TemplateResponse(
            request=request,
            name="catalog_error.html",
            context={"message": str(error)},
            status_code=400,
        )


@router.get("/catalog-fragment", response_class=HTMLResponse)
async def catalog_fragment(
    request: Request,
    q: str = "",
    language: str | None = None,
    genre: str | None = None,
    original_format: str | None = None,
    cursor: str | None = None,
) -> Response:
    catalog_request = _catalog_request(q, language, genre, original_format, cursor)
    try:
        context = await _results_context(request, catalog_request)
    except CatalogInputError as error:
        return templates.TemplateResponse(
            request=request,
            name="partials/catalog_error.html",
            context={"message": str(error)},
            status_code=400,
        )
    response = templates.TemplateResponse(
        request=request,
        name="partials/catalog_results.html",
        context=context,
    )
    response.headers["HX-Push-Url"] = _catalog_url("/", catalog_request, catalog_request.cursor)
    return response


@router.get("/books/{public_id}", response_class=HTMLResponse)
async def book_detail(request: Request, public_id: str) -> Response:
    book = await _catalog(request).details(public_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    return templates.TemplateResponse(
        request=request,
        name="book_detail.html",
        context={"book": book},
    )


async def _status_response(
    request: Request,
    status: ImportStatus | None,
    *,
    status_code: int = 200,
    message: str | None = None,
    poll: bool = False,
    pending: bool = False,
    poll_after_run_id: int | None = None,
) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="partials/import_status.html",
        context={
            "status": status,
            "ImportState": ImportState,
            "message": message,
            "poll": poll,
            "pending": pending,
            "poll_after_run_id": poll_after_run_id,
        },
        status_code=status_code,
    )


@router.get("/imports/status", response_class=HTMLResponse)
async def import_status(request: Request, after_run_id: int | None = None) -> Response:
    status = await _imports(request).get_status()
    if after_run_id is not None and (status is None or status.run_id <= after_run_id):
        return await _status_response(
            request,
            None,
            message="Manual import is starting",
            poll=True,
            pending=True,
            poll_after_run_id=after_run_id,
        )
    return await _status_response(request, status)


@router.post("/imports", response_class=HTMLResponse)
async def start_import(request: Request) -> Response:
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = cast(str, request.app.state.csrf_token)
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    coordinator = _imports(request)
    previous_status = await coordinator.get_status()
    accepted = coordinator.start_manual_import()
    if accepted:
        return await _status_response(
            request,
            None,
            status_code=202,
            message="Manual import is starting",
            poll=True,
            pending=True,
            poll_after_run_id=previous_status.run_id if previous_status is not None else 0,
        )
    return await _status_response(
        request,
        await coordinator.get_status(),
        status_code=409,
        message="An import is already running",
        poll=True,
    )


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/health-fragment", response_class=HTMLResponse)
async def health_fragment() -> HTMLResponse:
    return HTMLResponse('<span class="status-ok">Application is healthy</span>')
