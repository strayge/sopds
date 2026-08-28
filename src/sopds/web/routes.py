"""Server-rendered catalog and operational status routes."""

import asyncio
import logging
import secrets
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal, cast, override
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send

from sopds.acquisition.contracts import (
    AcquiredOriginal,
    Acquisition,
    AcquisitionAmbiguousMemberError,
    AcquisitionCorruptError,
    AcquisitionDirectoryMemberError,
    AcquisitionEncryptedMemberError,
    AcquisitionError,
    AcquisitionMemberNotFoundError,
    AcquisitionNotFoundError,
    AcquisitionSizeMismatchError,
    AcquisitionSourceIOError,
    AcquisitionStoreShutdownError,
    AcquisitionSymlinkMemberError,
    AcquisitionUnavailableError,
    AcquisitionUnsafePathError,
)
from sopds.acquisition.service import content_disposition
from sopds.catalog.contracts import (
    Catalog,
    CatalogFilters,
    CatalogInputError,
    CatalogPage,
    CatalogRequest,
    FilterOption,
    SearchField,
)
from sopds.imports.status import ImportState, ImportStatus, ImportStatusProvider

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
_LOGGER = logging.getLogger(__name__)
_WEB_PAGE_SIZE = 200
_NOT_FOUND_ERRORS = (
    AcquisitionNotFoundError,
    AcquisitionUnavailableError,
    AcquisitionMemberNotFoundError,
)
_INTERNAL_ERRORS = (
    AcquisitionUnsafePathError,
    AcquisitionAmbiguousMemberError,
    AcquisitionEncryptedMemberError,
    AcquisitionDirectoryMemberError,
    AcquisitionSymlinkMemberError,
    AcquisitionSizeMismatchError,
    AcquisitionCorruptError,
)


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]


def _format_author_name(value: str) -> str:
    return " ".join(part.strip() for part in value.split(",") if part.strip())


def _format_kilobytes(size: int) -> str:
    return f"{(size + 512) // 1024} KB"


def _format_integer(value: int) -> str:
    return f"{value:,}".replace(",", " ")


templates.env.filters["author_name"] = _format_author_name
templates.env.filters["kilobytes"] = _format_kilobytes
templates.env.filters["integer"] = _format_integer


def _catalog(request: Request) -> Catalog:
    return cast(Catalog, request.app.state.catalog)


def _imports(request: Request) -> ImportStatusProvider:
    return cast(ImportStatusProvider, request.app.state.import_coordinator)


def _acquisition(request: Request) -> Acquisition:
    return cast(Acquisition, request.app.state.acquisition)


def _shell_context(
    request: Request,
    *,
    active_navigation: Literal["catalog", "manage"],
) -> dict[str, object]:
    config = getattr(request.app.state, "config", None)
    opds_url = (
        str(config.server.base_url).rstrip("/") + "/opds/" if config is not None else "/opds/"
    )
    return {
        "request": request,
        "opds_url": opds_url,
        "active_navigation": active_navigation,
    }


def _catalog_request(
    q: str,
    search_field: SearchField,
    language: str | None,
    genre: str | None,
    original_format: str | None,
    author: str | None,
    series: str | None,
    include_missed: bool,
    include_hidden: bool,
    cursor: str | None,
) -> CatalogRequest:
    return CatalogRequest(
        query=q,
        search_field=search_field,
        language=language or None,
        genre=genre or None,
        original_format=original_format or None,
        author=author or None,
        series=series or None,
        include_missed=include_missed,
        include_hidden=include_hidden,
        cursor=cursor or None,
        page_size=_WEB_PAGE_SIZE,
    )


def _catalog_url(path: str, catalog_request: CatalogRequest, cursor: str | None) -> str:
    values = {
        "q": catalog_request.query,
        "search_field": catalog_request.search_field.value,
        "language": catalog_request.language or "",
        "genre": catalog_request.genre or "",
        "original_format": catalog_request.original_format or "",
        "cursor": cursor or "",
    }
    if catalog_request.author is not None:
        values["author"] = catalog_request.author
    if catalog_request.series is not None:
        values["series"] = catalog_request.series
    if catalog_request.include_missed:
        values["include_missed"] = "true"
    if catalog_request.include_hidden:
        values["include_hidden"] = "true"
    return f"{path}?{urlencode(values)}"


def _catalog_filter_state_context(catalog_request: CatalogRequest) -> dict[str, object]:
    return {
        "criteria_active": any(
            (
                bool(catalog_request.query),
                catalog_request.search_field is not SearchField.ALL,
                catalog_request.language is not None,
                catalog_request.genre is not None,
                catalog_request.original_format is not None,
                catalog_request.author is not None,
                catalog_request.series is not None,
                catalog_request.include_missed,
                catalog_request.include_hidden,
            )
        ),
    }


async def _catalog_form_context(
    request: Request, catalog_request: CatalogRequest
) -> dict[str, object]:
    filters = await _catalog(request).filters()
    languages = filters.languages
    genres = filters.genres
    original_formats = filters.original_formats
    if catalog_request.language is not None and all(
        option.value != catalog_request.language for option in languages
    ):
        languages += (FilterOption(catalog_request.language, catalog_request.language),)
    if catalog_request.genre is not None and all(
        option.value != catalog_request.genre for option in genres
    ):
        genres += (FilterOption(catalog_request.genre, catalog_request.genre),)
    if catalog_request.original_format is not None and all(
        option.value != catalog_request.original_format for option in original_formats
    ):
        original_formats += (
            FilterOption(catalog_request.original_format, catalog_request.original_format),
        )
    form_filters = CatalogFilters(
        languages=languages,
        genres=genres,
        original_formats=original_formats,
    )
    return {
        **_catalog_filter_state_context(catalog_request),
        "filters": form_filters,
        "author_removal_href": (
            _scope_removal_url(catalog_request, "author")
            if catalog_request.author is not None
            else None
        ),
        "series_removal_href": (
            _scope_removal_url(catalog_request, "series")
            if catalog_request.series is not None
            else None
        ),
    }


def _scope_removal_url(catalog_request: CatalogRequest, scope: Literal["author", "series"]) -> str:
    values = {
        "q": catalog_request.query,
        "search_field": catalog_request.search_field.value,
        "language": catalog_request.language or "",
        "genre": catalog_request.genre or "",
        "original_format": catalog_request.original_format or "",
    }
    if scope != "author" and catalog_request.author is not None:
        values["author"] = catalog_request.author
    if scope != "series" and catalog_request.series is not None:
        values["series"] = catalog_request.series
    if catalog_request.include_missed:
        values["include_missed"] = "true"
    if catalog_request.include_hidden:
        values["include_hidden"] = "true"
    return f"/?{urlencode(values)}"


def _next_urls(
    catalog_request: CatalogRequest, next_cursor: str | None
) -> tuple[str | None, str | None]:
    if next_cursor is None:
        return None, None
    return (
        _catalog_url("/", catalog_request, next_cursor),
        _catalog_url("/catalog-fragment", catalog_request, next_cursor),
    )


def _availability_query(include_missed: bool, include_hidden: bool) -> str:
    return urlencode(
        {
            name: "true"
            for name, enabled in (
                ("include_missed", include_missed),
                ("include_hidden", include_hidden),
            )
            if enabled
        }
    )


def _detail_query(catalog_request: CatalogRequest) -> str:
    values = {
        "return_to": _catalog_url("/", catalog_request, catalog_request.cursor),
    }
    if catalog_request.include_missed:
        values["include_missed"] = "true"
    if catalog_request.include_hidden:
        values["include_hidden"] = "true"
    return urlencode(values)


def _validated_return_url(value: str | None) -> str | None:
    if (
        not value
        or value.startswith("//")
        or "\\" in value
        or "#" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or any(digit not in "0123456789abcdefABCDEF" for digit in value[index + 1 : index + 3])
        ):
            return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme or parsed.netloc or parsed.path != "/" or parsed.fragment:
        return None
    return value


async def _results_context(
    request: Request,
    catalog_request: CatalogRequest,
    *,
    searched: bool = True,
) -> dict[str, object]:
    page = (
        await _catalog(request).browse(catalog_request)
        if searched
        else CatalogPage(books=(), next_cursor=None)
    )
    next_href, next_hx_url = _next_urls(catalog_request, page.next_cursor)
    return {
        "request": request,
        "page": page,
        "catalog_request": catalog_request,
        "next_href": next_href,
        "next_hx_url": next_hx_url,
        "availability_query": _availability_query(
            catalog_request.include_missed,
            catalog_request.include_hidden,
        ),
        "detail_query": _detail_query(catalog_request),
        "searched": searched,
    }


def _format_bytes(size: int) -> str:
    value = float(size)
    units = ("bytes", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "bytes":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("Database size unit bound was bypassed")


async def _statistics_context(request: Request) -> dict[str, object]:
    statistics = await _catalog(request).statistics()
    return {
        "request": request,
        "statistics": statistics,
        "database_size": _format_bytes(statistics.database_size_bytes),
    }


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    q: str = "",
    search_field: SearchField = SearchField.ALL,
    language: str | None = None,
    genre: str | None = None,
    original_format: str | None = None,
    author: str | None = None,
    series: str | None = None,
    include_missed: bool = False,
    include_hidden: bool = False,
    cursor: str | None = None,
) -> Response:
    catalog_request = _catalog_request(
        q,
        search_field,
        language,
        genre,
        original_format,
        author,
        series,
        include_missed,
        include_hidden,
        cursor,
    )
    searched = any(
        name in request.query_params
        for name in (
            "q",
            "search_field",
            "language",
            "genre",
            "original_format",
            "author",
            "series",
            "include_missed",
            "include_hidden",
            "cursor",
        )
    )
    try:
        context = await _results_context(request, catalog_request, searched=searched)
        context.update(_shell_context(request, active_navigation="catalog"))
        context.update(await _catalog_form_context(request, catalog_request))
        return templates.TemplateResponse(request=request, name="index.html", context=context)
    except CatalogInputError as error:
        return templates.TemplateResponse(
            request=request,
            name="catalog_error.html",
            context={
                **_shell_context(request, active_navigation="catalog"),
                "message": str(error),
            },
            status_code=400,
        )


@router.get("/manage", response_class=HTMLResponse)
async def manage(request: Request) -> Response:
    import_coordinator = _imports(request)
    current_import_status = await import_coordinator.get_status()
    import_pending = (
        current_import_status is None or current_import_status.state is not ImportState.RUNNING
    ) and import_coordinator.is_import_active()
    statistics_context: dict[str, object] | None
    status_code = 200
    try:
        statistics_context = await _statistics_context(request)
    except CatalogInputError:
        statistics_context = None
        status_code = 503
    return templates.TemplateResponse(
        request=request,
        name="manage.html",
        context={
            **_shell_context(request, active_navigation="manage"),
            "statistics_context": statistics_context,
            "status": None if import_pending else current_import_status,
            "csrf_token": cast(str, request.app.state.csrf_token),
            "ImportState": ImportState,
            "message": "Catalog import is starting" if import_pending else None,
            "poll": import_pending,
            "pending": import_pending,
            "poll_after_run_id": (
                current_import_status.run_id
                if import_pending and current_import_status is not None
                else None
            ),
        },
        status_code=status_code,
    )


@router.get("/catalog-statistics", response_class=HTMLResponse)
async def catalog_statistics(request: Request) -> Response:
    return templates.TemplateResponse(
        request=request,
        name="partials/catalog_statistics.html",
        context=await _statistics_context(request),
    )


@router.get("/catalog-fragment", response_class=HTMLResponse)
async def catalog_fragment(
    request: Request,
    q: str = "",
    search_field: SearchField = SearchField.ALL,
    language: str | None = None,
    genre: str | None = None,
    original_format: str | None = None,
    author: str | None = None,
    series: str | None = None,
    include_missed: bool = False,
    include_hidden: bool = False,
    cursor: str | None = None,
) -> Response:
    catalog_request = _catalog_request(
        q,
        search_field,
        language,
        genre,
        original_format,
        author,
        series,
        include_missed,
        include_hidden,
        cursor,
    )
    is_htmx = request.headers.get("HX-Request") == "true"
    push_url = _catalog_url("/", catalog_request, catalog_request.cursor)
    try:
        context = await _results_context(request, catalog_request)
    except CatalogInputError as error:
        error_context: dict[str, object] = {"message": str(error)}
        if is_htmx:
            try:
                error_context.update(await _catalog_form_context(request, catalog_request))
            except CatalogInputError:
                pass
            else:
                error_context["catalog_request"] = catalog_request
                error_context["include_catalog_form_oob"] = True
        response = templates.TemplateResponse(
            request=request,
            name="partials/catalog_error.html",
            context=error_context,
            status_code=200 if is_htmx else 400,
        )
        if is_htmx:
            response.headers["HX-Push-Url"] = push_url
        return response
    if is_htmx:
        try:
            context.update(await _catalog_form_context(request, catalog_request))
        except CatalogInputError as error:
            response = templates.TemplateResponse(
                request=request,
                name="partials/catalog_error.html",
                context={"message": str(error)},
            )
            response.headers["HX-Push-Url"] = push_url
            return response
        context["include_catalog_form_oob"] = True
    response = templates.TemplateResponse(
        request=request,
        name="partials/catalog_results.html",
        context=context,
    )
    response.headers["HX-Push-Url"] = push_url
    return response


@router.get("/books/{public_id}", response_class=HTMLResponse)
async def book_detail(
    request: Request,
    public_id: str,
    include_missed: bool = False,
    include_hidden: bool = False,
    return_to: str | None = None,
) -> Response:
    book = await _catalog(request).details(
        public_id,
        include_missed=include_missed,
        include_hidden=include_hidden,
    )
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    validated_return_url = _validated_return_url(return_to)
    return templates.TemplateResponse(
        request=request,
        name="book_detail.html",
        context={
            **_shell_context(request, active_navigation="catalog"),
            "book": book,
            "back_href": validated_return_url or "/",
            "back_label": (
                "Back to results" if validated_return_url is not None else "Back to catalog"
            ),
            "availability_query": _availability_query(include_missed, include_hidden),
        },
    )


async def _stream_original(original: AcquiredOriginal) -> AsyncIterator[bytes]:
    try:
        async for chunk in original.stream:
            yield chunk
    except AcquisitionError as error:
        _LOGGER.warning(
            f"Original download stream failed surface=web phase=stream "
            f"failure_type={type(error).__name__} response_started=True"
        )
        raise
    finally:
        await original.stream.aclose()


async def _close_owned_stream(original: AcquiredOriginal, *, response_started: bool) -> bool:
    """Finish response-owned cleanup even when its caller is repeatedly cancelled."""
    cleanup = asyncio.create_task(original.stream.aclose())
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    try:
        cleanup.result()
    except BaseException as error:
        _LOGGER.error(
            f"Original download cleanup failed surface=web phase=cleanup "
            f"failure_type={type(error).__name__} response_started={response_started}"
        )
    return cancelled


class _OwnedStreamingResponse(StreamingResponse):
    """Own an acquired stream for the complete ASGI response lifecycle."""

    def __init__(self, original: AcquiredOriginal, headers: dict[str, str]) -> None:
        self._original = original
        super().__init__(_stream_original(original), status_code=200, headers=headers)

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        response_started = False

        async def owned_send(message: Message) -> None:
            nonlocal response_started
            await send(message)
            if message["type"] == "http.response.start":
                response_started = True

        try:
            await super().__call__(scope, receive, owned_send)
        except BaseException:
            try:
                await _close_owned_stream(
                    self._original,
                    response_started=response_started,
                )
            finally:
                raise
        else:
            if await _close_owned_stream(
                self._original,
                response_started=response_started,
            ):
                raise asyncio.CancelledError


@router.get("/books/{public_id}/download")
async def download_original(request: Request, public_id: str) -> Response:
    try:
        original = await _acquisition(request).acquire(public_id)
    except _NOT_FOUND_ERRORS as error:
        raise HTTPException(status_code=404, detail="Original is unavailable") from error
    except AcquisitionStoreShutdownError as error:
        raise HTTPException(status_code=503, detail="Service is shutting down") from error
    except AcquisitionSourceIOError as error:
        _LOGGER.warning(
            f"Original download source I/O failed surface=web phase=open "
            f"failure_type={type(error).__name__}"
        )
        raise HTTPException(status_code=500, detail="Original cannot be served") from error
    except _INTERNAL_ERRORS as error:
        _LOGGER.warning(
            f"Original download integrity check failed surface=web phase=open "
            f"failure_type={type(error).__name__}"
        )
        raise HTTPException(status_code=500, detail="Original cannot be served") from error
    except AcquisitionError as error:
        _LOGGER.warning(
            f"Original download failed surface=web phase=open failure_type={type(error).__name__}"
        )
        raise HTTPException(status_code=500, detail="Original cannot be served") from error

    try:
        headers = {
            "Content-Type": original.media_type,
            "Content-Length": str(original.content_length),
            "Content-Disposition": content_disposition(original.filename),
            "X-Content-Type-Options": "nosniff",
        }
        return _OwnedStreamingResponse(original, headers)
    except BaseException:
        await original.stream.aclose()
        raise


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
    coordinator = _imports(request)
    status = await coordinator.get_status()
    if after_run_id is not None and (status is None or status.run_id <= after_run_id):
        if not coordinator.is_import_active():
            return await _status_response(
                request,
                status,
                message="No catalog changes found",
            )
        return await _status_response(
            request,
            None,
            message="Manual import is starting",
            poll=True,
            pending=True,
            poll_after_run_id=after_run_id,
        )
    if status is None and coordinator.is_import_active():
        return await _status_response(
            request,
            None,
            message="Catalog import is starting",
            poll=True,
            pending=True,
        )
    response = await _status_response(
        request,
        status,
        poll_after_run_id=after_run_id,
    )
    if (
        after_run_id is not None
        and status is not None
        and status.run_id > after_run_id
        and status.state is not ImportState.RUNNING
    ):
        response.headers["HX-Trigger"] = "catalogChanged"
    return response


def _validate_csrf(request: Request) -> None:
    supplied = request.headers.get("X-CSRF-Token", "")
    expected = cast(str, request.app.state.csrf_token)
    if not supplied or not secrets.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")


async def _start_import(request: Request, *, force: bool) -> Response:
    _validate_csrf(request)
    coordinator = _imports(request)
    previous_status = await coordinator.get_status()
    accepted = coordinator.start_manual_import(force=force)
    if accepted:
        mode = "Force import" if force else "Import check"
        return await _status_response(
            request,
            None,
            status_code=202,
            message=f"{mode} is starting",
            poll=True,
            pending=True,
            poll_after_run_id=previous_status.run_id if previous_status is not None else 0,
        )
    return await _status_response(
        request,
        await coordinator.get_status(),
        message="An import or database maintenance operation is already running",
        poll=coordinator.is_import_active(),
    )


@router.post("/imports", response_class=HTMLResponse)
async def start_import(request: Request) -> Response:
    return await _start_import(request, force=False)


@router.post("/imports/force", response_class=HTMLResponse)
async def start_force_import(request: Request) -> Response:
    return await _start_import(request, force=True)


@router.post("/database/vacuum", response_class=HTMLResponse)
async def vacuum_database(request: Request) -> Response:
    _validate_csrf(request)
    vacuumed = await _imports(request).vacuum_database()
    response = templates.TemplateResponse(
        request=request,
        name="partials/operation_result.html",
        context={
            "message": (
                "Database VACUUM completed"
                if vacuumed
                else "VACUUM skipped because catalog work is running"
            ),
            "error": not vacuumed,
        },
    )
    if vacuumed:
        response.headers["HX-Trigger"] = "catalogChanged"
    return response


async def _database_ready(request: Request, endpoint: str) -> bool:
    try:
        await _catalog(request).check_readiness()
    except Exception as error:
        _LOGGER.warning(
            f"Database readiness check failed component={endpoint} "
            f"failure_type={type(error).__name__}"
        )
        return False
    return True


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> Response:
    if await _database_ready(request, "/health"):
        return JSONResponse(HealthResponse(status="ok").model_dump(), status_code=200)
    return JSONResponse(HealthResponse(status="unavailable").model_dump(), status_code=503)
