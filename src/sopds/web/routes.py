"""Server-rendered catalog and operational status routes."""

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal, cast, override
from urllib.parse import parse_qsl, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.requests import ClientDisconnect
from starlette.types import Message, Receive, Scope, Send

from sopds.acquisition.archive import (
    ArchiveError,
    ArchiveInputError,
    ArchiveLimitError,
    ArchiveManifest,
    ArchiveRequest,
    ArchiveService,
    StagedArchive,
)
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
    BookSummary,
    Catalog,
    CatalogFilters,
    CatalogInputError,
    CatalogPage,
    CatalogRequest,
    FilterOption,
    SearchField,
)
from sopds.catalog.search import normalize_text
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
    normalize_format,
)
from sopds.conversion.policy import OUTPUT_POLICY, OutputChoice, OutputDecision
from sopds.conversion.registry import ConverterRegistry
from sopds.conversion.service import ConversionService
from sopds.imports.status import ImportState, ImportStatus, ImportStatusProvider
from sopds.web.csrf import issue_csrf_token, validate_csrf_token
from sopds.web.i18n import (
    catalog_browser_messages,
    catalog_error_message,
    import_state_label,
    import_trigger_label,
    known_html_message,
    request_translation_context,
    selection_browser_messages,
)

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
_LOGGER = logging.getLogger(__name__)
_WEB_RESULT_LIMIT = 1_000
_MAX_SELECTED_BODY_BYTES = 8_388_608
_SELECTED_ARCHIVE_FILENAME = "selected-books.zip"
_CSRF_ERROR_MESSAGE = "This page has expired. Reload it and try again."
_READER_SOURCE_LIMIT = 64 * 1024 * 1024
_READER_CSP = "; ".join(
    (
        "default-src 'none'",
        "script-src 'self'",
        "script-src-attr 'none'",
        "style-src 'self' 'unsafe-inline' blob:",
        "img-src data: blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "frame-src blob:",
        "worker-src 'none'",
        "media-src 'none'",
        "object-src 'none'",
        "manifest-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
    )
)
_READER_HEADERS = {"Content-Security-Policy": _READER_CSP}
_PUBLIC_ARCHIVE_INPUT_MESSAGES = frozenset(
    {
        "Invalid archive preset",
        "Invalid archive format",
        "Invalid archive request",
        "Invalid selected book IDs",
        "Invalid public book ID",
        "Too many selected books",
        "Selected books exceed the source-size limit",
        "No selected books are available for download",
    }
)
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


def _source_format_label(value: str) -> str:
    try:
        return normalize_format(value).upper()
    except ValueError:
        return "FILE"


templates.env.filters["author_name"] = _format_author_name
templates.env.filters["kilobytes"] = _format_kilobytes
templates.env.filters["integer"] = _format_integer
templates.env.filters["source_format_label"] = _source_format_label


def _merge_vary(response: Response, *names: str) -> None:
    values = [value.strip() for value in response.headers.get("Vary", "").split(",")]
    merged = [value for value in values if value]
    existing = {value.casefold() for value in merged}
    for name in names:
        if name.casefold() not in existing:
            merged.append(name)
            existing.add(name.casefold())
    response.headers["Vary"] = ", ".join(merged)


def _localized_template_response(
    request: Request,
    name: str,
    context: dict[str, object] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> Response:
    """Bind translations to one render while preserving route response headers."""
    translations = request_translation_context(request)
    render_context = dict(context or {})
    render_context.update(
        {
            "locale": translations.locale,
            "gettext": translations.gettext,
            "ngettext": translations.ngettext,
            "pgettext": translations.pgettext,
            "npgettext": translations.npgettext,
        }
    )
    response = templates.TemplateResponse(
        request=request,
        name=name,
        context=render_context,
        status_code=status_code,
        headers=headers,
    )
    _merge_vary(response, "Cookie", "Accept-Language")
    return response


def _known_html_message(request: Request, source: str) -> str:
    return known_html_message(request_translation_context(request), source)


def _catalog_error_message(request: Request, error: CatalogInputError) -> str:
    return catalog_error_message(request_translation_context(request), error)


def _status_presentation(request: Request, status: ImportStatus | None) -> dict[str, str]:
    if status is None:
        return {}
    translations = request_translation_context(request)
    return {
        "state_label": import_state_label(translations, status.state),
        "trigger_label": import_trigger_label(translations, status.trigger),
    }


def _catalog(request: Request) -> Catalog:
    return cast(Catalog, request.app.state.catalog)


def _imports(request: Request) -> ImportStatusProvider:
    return cast(ImportStatusProvider, request.app.state.import_coordinator)


def _acquisition(request: Request) -> Acquisition:
    return cast(Acquisition, request.app.state.acquisition)


def _archive(request: Request) -> ArchiveService:
    return cast(ArchiveService, request.app.state.archive)


def _conversion(request: Request) -> ConversionService:
    return cast(ConversionService, request.app.state.conversion)


def _converter_registry(request: Request) -> ConverterRegistry | None:
    return cast(ConverterRegistry | None, getattr(request.app.state, "converter_registry", None))


def _additional_download_formats(request: Request, source_format: str) -> tuple[OutputChoice, ...]:
    registry = _converter_registry(request)
    if registry is None:
        return ()
    targets: list[OutputChoice] = []
    for choice in OUTPUT_POLICY.choices():
        if OUTPUT_POLICY.decision(source_format, choice.key) is not OutputDecision.CONVERT:
            continue
        try:
            registry.resolve(source_format, choice.key)
        except UnsupportedConversionError, ValueError:
            continue
        targets.append(choice)
    return tuple(targets)


def _shell_context(
    request: Request,
    *,
    active_navigation: Literal["catalog", "selected", "manage"],
) -> dict[str, object]:
    config = getattr(request.app.state, "config", None)
    opds_url = (
        str(config.server.base_url).rstrip("/") + "/opds/" if config is not None else "/opds/"
    )
    translations = request_translation_context(request)
    return {
        "request": request,
        "opds_url": opds_url,
        "active_navigation": active_navigation,
        "selection_messages": selection_browser_messages(translations),
    }


def _catalog_request(
    q: str,
    search_field: SearchField,
    language: str | None,
    genre: str | None,
    original_format: str | None,
    include_missed: bool,
    include_hidden: bool,
) -> CatalogRequest:
    return CatalogRequest(
        query=q,
        search_field=search_field,
        language=language or None,
        genre=genre or None,
        original_format=original_format or None,
        include_missed=include_missed,
        include_hidden=include_hidden,
        cursor=None,
        page_size=_WEB_RESULT_LIMIT,
    )


def _catalog_url(path: str, catalog_request: CatalogRequest) -> str:
    values = {
        "q": catalog_request.query,
        "search_field": catalog_request.search_field.value,
        "language": catalog_request.language or "",
        "genre": catalog_request.genre or "",
        "original_format": catalog_request.original_format or "",
    }
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
    }


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


def _availability_suffix(include_missed: bool, include_hidden: bool) -> str:
    query = _availability_query(include_missed, include_hidden)
    return f"?{query}" if query else ""


def _metadata_search_url(
    search_field: Literal["author", "series"],
    query: str,
    catalog_request: CatalogRequest,
) -> str:
    values = {
        "q": query,
        "search_field": search_field,
        "language": catalog_request.language or "",
        "genre": catalog_request.genre or "",
        "original_format": catalog_request.original_format or "",
    }
    if catalog_request.include_missed:
        values["include_missed"] = "true"
    if catalog_request.include_hidden:
        values["include_hidden"] = "true"
    return f"/?{urlencode(values)}"


def _normalized_source_format(value: str) -> str | None:
    try:
        return normalize_format(value)
    except ValueError:
        return None


def _catalog_book_payload(
    request: Request,
    book: BookSummary,
    catalog_request: CatalogRequest,
) -> dict[str, object]:
    path_id = quote(book.public_id, safe="")
    suffix = _availability_suffix(
        catalog_request.include_missed,
        catalog_request.include_hidden,
    )
    available_download = book.downloadable and book.availability.value != "missed"
    source_format = _normalized_source_format(book.original_format)
    read_url = (
        f"/books/{path_id}/read{suffix}"
        if available_download
        and source_format in {"fb2", "epub"}
        and book.size <= _READER_SOURCE_LIMIT
        else None
    )
    conversions = (
        [
            {
                "url": f"/books/{path_id}/download/{choice.key}",
                "label": choice.label,
            }
            for choice in _additional_download_formats(request, book.original_format)
        ]
        if available_download
        else []
    )
    return {
        "publicId": book.public_id,
        "title": book.title,
        "titleSortKey": normalize_text(book.title),
        "authors": [
            {
                "raw": author,
                "display": _format_author_name(author),
                "sortKey": normalize_text(author),
                "scopeUrl": _metadata_search_url(
                    "author",
                    _format_author_name(author),
                    catalog_request,
                ),
            }
            for author in book.authors
        ],
        "series": (
            {
                "name": book.series,
                "sortKey": normalize_text(book.series),
                "number": book.series_number,
                "scopeUrl": _metadata_search_url(
                    "series",
                    book.series,
                    catalog_request,
                ),
            }
            if book.series is not None
            else None
        ),
        "language": book.language,
        "sourceFormat": {
            "key": source_format,
            "label": _source_format_label(book.original_format),
        },
        "size": book.size,
        "sizeLabel": _format_kilobytes(book.size),
        "publishedDate": (
            book.published_date.isoformat() if book.published_date is not None else None
        ),
        "availability": book.availability.value,
        "selectable": available_download,
        "downloadable": available_download,
        "detailUrl": f"/books/{path_id}{suffix}",
        "readUrl": read_url,
        "originalDownload": (
            {
                "url": f"/books/{path_id}/download",
                "label": _source_format_label(book.original_format),
            }
            if available_download
            else None
        ),
        "conversions": conversions,
    }


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
    translations = request_translation_context(request)
    return {
        "request": request,
        "page": page,
        "catalog_request": catalog_request,
        "catalog_payload": {
            "books": [_catalog_book_payload(request, book, catalog_request) for book in page.books],
            "truncated": page.next_cursor is not None,
        },
        "catalog_locale": translations.locale,
        "catalog_messages": catalog_browser_messages(translations),
        "truncated": page.next_cursor is not None,
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


templates.env.filters["filesize"] = _format_bytes


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
    include_missed: bool = False,
    include_hidden: bool = False,
) -> Response:
    catalog_request = _catalog_request(
        q,
        search_field,
        language,
        genre,
        original_format,
        include_missed,
        include_hidden,
    )
    searched = any(
        name in request.query_params
        for name in (
            "q",
            "search_field",
            "language",
            "genre",
            "original_format",
            "include_missed",
            "include_hidden",
        )
    )
    try:
        context = await _results_context(request, catalog_request, searched=searched)
        context.update(_shell_context(request, active_navigation="catalog"))
        context.update(await _catalog_form_context(request, catalog_request))
        return _localized_template_response(request, "index.html", context)
    except CatalogInputError as error:
        return _localized_template_response(
            request,
            "catalog_error.html",
            context={
                **_shell_context(request, active_navigation="catalog"),
                "message": _catalog_error_message(request, error),
            },
            status_code=400,
        )


class _SelectedBodyError(ValueError):
    """Reject transport syntax without reflecting submitted values."""


class _SelectedBodyTooLargeError(_SelectedBodyError):
    """Stop consuming a selected-books request as soon as it exceeds its bound."""


class _CsrfError(ValueError):
    """Reject missing, malformed, expired, or restart-invalidated browser tokens."""


def _issue_csrf(request: Request) -> str:
    return issue_csrf_token(cast(bytes, request.app.state.csrf_key))


def _validate_csrf(request: Request, supplied: str) -> None:
    key = cast(bytes, request.app.state.csrf_key)
    if not validate_csrf_token(key, supplied):
        raise _CsrfError


async def _read_selected_body(request: Request) -> bytes:
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > _MAX_SELECTED_BODY_BYTES:
                raise _SelectedBodyTooLargeError
            body.extend(chunk)
    except ClientDisconnect as error:
        raise _SelectedBodyError from error
    return bytes(body)


def _media_type(request: Request) -> str:
    return request.headers.get("content-type", "").partition(";")[0].strip().casefold()


def _reject_json_constant(_value: str) -> object:
    raise ValueError


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


async def _json_archive_request(request: Request) -> ArchiveRequest:
    if _media_type(request) != "application/json":
        raise _SelectedBodyError
    body = await _read_selected_body(request)
    try:
        text = body.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise _SelectedBodyError from error
    return ArchiveRequest.from_input(value)


def _validate_form_percent_encoding(value: str) -> None:
    hexadecimal = frozenset("0123456789abcdefABCDEF")
    for index, character in enumerate(value):
        if character == "%" and (
            index + 2 >= len(value)
            or value[index + 1] not in hexadecimal
            or value[index + 2] not in hexadecimal
        ):
            raise _SelectedBodyError


async def _form_archive_request(request: Request) -> ArchiveRequest:
    if _media_type(request) != "application/x-www-form-urlencoded":
        raise _SelectedBodyError
    body = await _read_selected_body(request)
    try:
        encoded = body.decode("ascii", errors="strict")
        _validate_form_percent_encoding(encoded)
        pairs = parse_qsl(
            encoded,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=4,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise _SelectedBodyError from error
    names = [name for name, _value in pairs]
    if len(names) != len(set(names)) or not set(names) <= {
        "ids",
        "preset",
        "format",
        "csrf_token",
    }:
        raise _SelectedBodyError
    fields = dict(pairs)
    _validate_csrf(request, fields.get("csrf_token", ""))
    if set(fields) not in (
        {"ids", "preset", "csrf_token"},
        {"ids", "preset", "format", "csrf_token"},
    ):
        raise _SelectedBodyError
    try:
        ids = json.loads(fields["ids"], parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, ValueError, RecursionError) as error:
        raise _SelectedBodyError from error
    archive_fields = {"ids": ids, "preset": fields["preset"]}
    if "format" in fields:
        archive_fields["format"] = fields["format"]
    return ArchiveRequest.from_input(archive_fields)


def _selected_error_response(
    request: Request,
    *,
    message: str,
    status_code: int,
    fragment: bool,
) -> Response:
    message = _known_html_message(request, message)
    if fragment:
        return _localized_template_response(
            request,
            "partials/selected_preview.html",
            context={"manifest": None, "message": message},
            status_code=status_code,
        )
    return _localized_template_response(
        request,
        "selected_error.html",
        context={
            **_shell_context(request, active_navigation="selected"),
            "message": message,
        },
        status_code=status_code,
    )


def _selected_transport_error(
    request: Request, error: _SelectedBodyError, *, fragment: bool
) -> Response:
    if isinstance(error, _SelectedBodyTooLargeError):
        return _selected_error_response(
            request,
            message="Selected-books request is too large",
            status_code=413,
            fragment=fragment,
        )
    return _selected_error_response(
        request,
        message="Invalid selected-books request",
        status_code=400,
        fragment=fragment,
    )


def _selected_input_error(
    request: Request, error: ArchiveInputError, *, fragment: bool
) -> Response:
    status_code = 413 if isinstance(error, ArchiveLimitError) else 422
    detail = str(error)
    message = (
        detail if detail in _PUBLIC_ARCHIVE_INPUT_MESSAGES else "Invalid selected-books request"
    )
    return _selected_error_response(
        request,
        message=message,
        status_code=status_code,
        fragment=fragment,
    )


@router.get("/selected", response_class=HTMLResponse)
async def selected(request: Request) -> Response:
    return _localized_template_response(
        request,
        "selected.html",
        context={
            **_shell_context(request, active_navigation="selected"),
            "csrf_token": _issue_csrf(request),
        },
        headers={"Cache-Control": "no-store"},
    )


def _selected_row_action_context(
    request: Request,
    manifest: ArchiveManifest,
) -> dict[str, object]:
    additional_download_formats: dict[str, tuple[OutputChoice, ...]] = {}
    read_urls: dict[str, str] = {}
    for entry in manifest.entries:
        book = entry.summary
        if book is None or entry.status.value not in {"downloadable", "unsupported"}:
            continue
        additional_download_formats[book.public_id] = _additional_download_formats(
            request, book.original_format
        )
        if (
            _normalized_source_format(book.original_format) in {"fb2", "epub"}
            and book.size <= _READER_SOURCE_LIMIT
        ):
            read_url, _download_url, _detail_url = _reader_book_urls(
                book.public_id,
                include_missed=False,
                include_hidden=book.availability.value == "hidden",
            )
            read_urls[book.public_id] = read_url
    return {
        "additional_download_formats": additional_download_formats,
        "read_urls": read_urls,
    }


@router.post("/selected/preview", response_class=HTMLResponse)
async def selected_preview(request: Request) -> Response:
    try:
        archive_request = await _json_archive_request(request)
    except _SelectedBodyError as error:
        return _selected_transport_error(request, error, fragment=True)
    except ArchiveInputError as error:
        return _selected_input_error(request, error, fragment=True)

    try:
        manifest = await _archive(request).preview(archive_request)
    except ArchiveInputError as error:
        return _selected_input_error(request, error, fragment=True)
    except CatalogInputError:
        return _selected_error_response(
            request,
            message="Catalog changed while loading; retry the request",
            status_code=422,
            fragment=True,
        )
    except Exception as error:
        _LOGGER.warning(
            f"Selected-books preview failed surface=web phase=preview "
            f"failure_type={type(error).__name__}"
        )
        return _selected_error_response(
            request,
            message="The selected-books preview is unavailable",
            status_code=500,
            fragment=True,
        )
    return _localized_template_response(
        request,
        "partials/selected_preview.html",
        context={
            "manifest": manifest,
            "message": None,
            **_selected_row_action_context(request, manifest),
        },
    )


async def _close_staged_archive(staged: StagedArchive, *, response_started: bool) -> bool:
    """Finish archive cleanup even when its response task is repeatedly cancelled."""
    cleanup = asyncio.create_task(staged.aclose())
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
            f"Selected-books archive cleanup failed surface=web phase=cleanup "
            f"failure_type={type(error).__name__} response_started={response_started}"
        )
    return cancelled


async def _wait_for_disconnect(request: Request) -> None:
    while True:
        if (await request.receive())["type"] == "http.disconnect":
            return


async def _drain_route_task[T](task: asyncio.Task[T]) -> bool:
    """Consume a task while recording cancellation of the owning route."""
    current = asyncio.current_task()
    cancelled = current is not None and current.cancelling() > 0
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            cancelled = cancelled or (current is not None and current.cancelling() > 0)
        except BaseException:
            break
    return cancelled


async def _discard_archive_build(build: asyncio.Task[StagedArchive], *, cancel: bool) -> bool:
    """Drain an abandoned build and close any archive produced by a completion race."""
    if cancel and not build.done():
        build.cancel()
    cancelled = await _drain_route_task(build)
    if build.cancelled():
        return cancelled
    try:
        staged = build.result()
    except BaseException:
        return cancelled
    close_cancelled = await _close_staged_archive(staged, response_started=False)
    return cancelled or close_cancelled


async def _download_while_connected(
    request: Request, archive_request: ArchiveRequest
) -> StagedArchive | None:
    build = asyncio.create_task(_archive(request).download(archive_request))
    disconnect = asyncio.create_task(_wait_for_disconnect(request))
    try:
        completed, _pending = await asyncio.wait(
            {build, disconnect}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        disconnect.cancel()
        build.cancel()
        await _drain_route_task(disconnect)
        await _discard_archive_build(build, cancel=False)
        raise

    if disconnect in completed:
        disconnect_error: BaseException | None = None
        try:
            disconnect.result()
        except BaseException as error:
            disconnect_error = error
        cancelled = await _discard_archive_build(build, cancel=True)
        if cancelled:
            raise asyncio.CancelledError
        if disconnect_error is not None:
            raise disconnect_error
        return None

    disconnect.cancel()
    cancelled = await _drain_route_task(disconnect)
    if cancelled:
        await _discard_archive_build(build, cancel=False)
        raise asyncio.CancelledError
    return build.result()


class _ClientDisconnectedResponse(Response):
    """Complete routing without sending after transport closure is confirmed."""

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        return None


class _OwnedStagedArchiveResponse(StreamingResponse):
    """Own a staged archive for the complete ASGI response lifecycle."""

    def __init__(self, staged: StagedArchive, headers: dict[str, str]) -> None:
        self._staged = staged
        super().__init__(staged, status_code=200, headers=headers)

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
        except BaseException as error:
            if not isinstance(error, asyncio.CancelledError):
                _LOGGER.warning(
                    f"Selected-books archive response failed surface=web phase=stream "
                    f"failure_type={type(error).__name__} response_started={response_started}"
                )
            cancelled = await _close_staged_archive(
                self._staged,
                response_started=response_started,
            )
            if isinstance(error, asyncio.CancelledError):
                raise
            if cancelled:
                raise asyncio.CancelledError from error
            raise
        else:
            if await _close_staged_archive(
                self._staged,
                response_started=response_started,
            ):
                raise asyncio.CancelledError


@router.post("/selected/download")
async def selected_download(request: Request) -> Response:
    try:
        archive_request = await _form_archive_request(request)
    except _CsrfError:
        return _selected_error_response(
            request,
            message=_CSRF_ERROR_MESSAGE,
            status_code=403,
            fragment=False,
        )
    except _SelectedBodyError as error:
        return _selected_transport_error(request, error, fragment=False)
    except ArchiveInputError as error:
        return _selected_input_error(request, error, fragment=False)

    try:
        staged = await _download_while_connected(request, archive_request)
    except ArchiveInputError as error:
        return _selected_input_error(request, error, fragment=False)
    except CatalogInputError:
        return _selected_error_response(
            request,
            message="Catalog changed while loading; retry the request",
            status_code=422,
            fragment=False,
        )
    except AcquisitionStoreShutdownError:
        return _selected_error_response(
            request,
            message="Service is shutting down",
            status_code=503,
            fragment=False,
        )
    except AcquisitionError as error:
        _LOGGER.warning(
            f"Selected-books archive acquisition failed surface=web phase=build "
            f"failure_type={type(error).__name__}"
        )
        return _selected_error_response(
            request,
            message="The selected books archive could not be created",
            status_code=500,
            fragment=False,
        )
    except ArchiveError as error:
        _LOGGER.warning(
            f"Selected-books archive build failed surface=web phase=build "
            f"failure_type={type(error).__name__}"
        )
        return _selected_error_response(
            request,
            message="The selected books archive could not be created",
            status_code=500,
            fragment=False,
        )
    except Exception as error:
        _LOGGER.warning(
            f"Selected-books archive failed surface=web phase=build "
            f"failure_type={type(error).__name__}"
        )
        return _selected_error_response(
            request,
            message="The selected books archive could not be created",
            status_code=500,
            fragment=False,
        )

    if staged is None:
        return _ClientDisconnectedResponse()

    archive_filename = (
        _SELECTED_ARCHIVE_FILENAME
        if archive_request.format == "original"
        else f"selected-books-{archive_request.format}.zip"
    )
    headers = {
        "Content-Type": "application/zip",
        "Content-Length": str(staged.content_length),
        "Content-Disposition": content_disposition(archive_filename),
        "X-Content-Type-Options": "nosniff",
    }
    try:
        return _OwnedStagedArchiveResponse(staged, headers)
    except BaseException as error:
        cancelled = await _close_staged_archive(staged, response_started=False)
        if isinstance(error, asyncio.CancelledError):
            raise
        if cancelled:
            raise asyncio.CancelledError from error
        if not isinstance(error, Exception):
            raise
        _LOGGER.warning(
            f"Selected-books archive response creation failed surface=web "
            f"phase=response failure_type={type(error).__name__}"
        )
        return _selected_error_response(
            request,
            message="The selected books archive could not be created",
            status_code=500,
            fragment=False,
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
    return _localized_template_response(
        request,
        "manage.html",
        context={
            **_shell_context(request, active_navigation="manage"),
            "statistics_context": statistics_context,
            "status": None if import_pending else current_import_status,
            "csrf_token": _issue_csrf(request),
            "ImportState": ImportState,
            **_status_presentation(request, None if import_pending else current_import_status),
            "message": (
                _known_html_message(request, "Catalog import is starting")
                if import_pending
                else None
            ),
            "poll": import_pending,
            "pending": import_pending,
            "poll_after_run_id": (
                current_import_status.run_id
                if import_pending and current_import_status is not None
                else None
            ),
        },
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/catalog-statistics", response_class=HTMLResponse)
async def catalog_statistics(request: Request) -> Response:
    return _localized_template_response(
        request,
        "partials/catalog_statistics.html",
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
    include_missed: bool = False,
    include_hidden: bool = False,
) -> Response:
    catalog_request = _catalog_request(
        q,
        search_field,
        language,
        genre,
        original_format,
        include_missed,
        include_hidden,
    )
    is_htmx = request.headers.get("HX-Request") == "true"
    push_url = _catalog_url("/", catalog_request)
    try:
        context = await _results_context(request, catalog_request)
    except CatalogInputError as error:
        error_context: dict[str, object] = {"message": _catalog_error_message(request, error)}
        if is_htmx:
            try:
                error_context.update(await _catalog_form_context(request, catalog_request))
            except CatalogInputError:
                pass
            else:
                error_context["catalog_request"] = catalog_request
                error_context["include_catalog_form_oob"] = True
        response = _localized_template_response(
            request,
            "partials/catalog_error.html",
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
            response = _localized_template_response(
                request,
                "partials/catalog_error.html",
                context={"message": _catalog_error_message(request, error)},
            )
            response.headers["HX-Push-Url"] = push_url
            return response
        context["include_catalog_form_oob"] = True
    response = _localized_template_response(
        request,
        "partials/catalog_results.html",
        context=context,
    )
    response.headers["HX-Push-Url"] = push_url
    return response


def _reader_book_urls(
    public_id: str,
    *,
    include_missed: bool,
    include_hidden: bool,
) -> tuple[str, str, str]:
    path_id = quote(public_id, safe="")
    values: dict[str, str] = {}
    if include_missed:
        values["include_missed"] = "true"
    if include_hidden:
        values["include_hidden"] = "true"
    query = urlencode(values)
    suffix = f"?{query}" if query else ""
    return (
        f"/books/{path_id}/read{suffix}",
        f"/books/{path_id}/download",
        f"/books/{path_id}{suffix}",
    )


def _reader_source_format(value: str) -> str | None:
    try:
        source_format = normalize_format(value)
    except ValueError:
        return None
    return source_format if source_format in {"fb2", "epub"} else None


def _source_revision_token(original: AcquiredOriginal) -> str:
    revision = original.source_revision
    identity = (
        f"{revision.archive_size}:{revision.archive_mtime_ns}:{revision.member_crc32}"
    ).encode()
    return hashlib.sha256(identity).hexdigest()


@router.get("/books/{public_id}", response_class=HTMLResponse)
async def book_detail(
    request: Request,
    public_id: str,
    include_missed: bool = False,
    include_hidden: bool = False,
) -> Response:
    book = await _catalog(request).details(
        public_id,
        include_missed=include_missed,
        include_hidden=include_hidden,
    )
    if book is None:
        raise HTTPException(status_code=404, detail="Book not found")
    source_format = _reader_source_format(book.original_format)
    reader_url = None
    if book.downloadable and book.availability.value != "missed" and source_format is not None:
        reader_url, _download_url, _detail_url = _reader_book_urls(
            book.public_id,
            include_missed=include_missed,
            include_hidden=include_hidden,
        )
    return _localized_template_response(
        request,
        "book_detail.html",
        context={
            **_shell_context(request, active_navigation="catalog"),
            "book": book,
            "reader_url": reader_url,
            "availability_query": _availability_query(include_missed, include_hidden),
            "additional_download_formats": _additional_download_formats(
                request, book.original_format
            ),
        },
    )


@router.get("/books/{public_id}/read", response_class=HTMLResponse)
async def book_reader(
    request: Request,
    public_id: str,
    include_missed: bool = False,
    include_hidden: bool = False,
) -> Response:
    book = await _catalog(request).details(
        public_id,
        include_missed=include_missed,
        include_hidden=include_hidden,
    )
    source_format = None if book is None else _reader_source_format(book.original_format)
    if (
        book is None
        or not book.downloadable
        or book.availability.value == "missed"
        or source_format is None
    ):
        raise HTTPException(
            status_code=404,
            detail="Book not found",
            headers=_READER_HEADERS,
        )

    retry_url, download_url, detail_url = _reader_book_urls(
        book.public_id,
        include_missed=include_missed,
        include_hidden=include_hidden,
    )
    over_limit = book.size > _READER_SOURCE_LIMIT
    return templates.TemplateResponse(
        request=request,
        name="book_reader.html",
        context={
            "book": book,
            "source_format": source_format,
            "source_url": download_url,
            "retry_url": retry_url,
            "download_url": download_url,
            "detail_url": detail_url,
            "reader_error": (
                "This book is larger than the 64 MiB web reader limit. "
                "You can still download the original file."
                if over_limit
                else None
            ),
        },
        headers=_READER_HEADERS,
    )


async def _stream_original(
    original: AcquiredOriginal | ConversionResult,
) -> AsyncIterator[bytes]:
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


async def _close_owned_stream(
    original: AcquiredOriginal | ConversionResult, *, response_started: bool
) -> bool:
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

    def __init__(
        self, original: AcquiredOriginal | ConversionResult, headers: dict[str, str]
    ) -> None:
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


async def _discard_conversion(conversion: asyncio.Task[ConversionResult], *, cancel: bool) -> bool:
    """Drain an abandoned conversion and close any artifact won in a completion race."""
    if cancel and not conversion.done():
        conversion.cancel()
    cancelled = await _drain_route_task(conversion)
    if conversion.cancelled():
        return cancelled
    try:
        result = conversion.result()
    except BaseException:
        return cancelled
    close_cancelled = await _close_owned_stream(result, response_started=False)
    return cancelled or close_cancelled


async def _convert_while_connected(
    request: Request, public_id: str, target_format: str
) -> ConversionResult | None:
    conversion = asyncio.create_task(_conversion(request).convert(public_id, target_format))
    disconnect = asyncio.create_task(_wait_for_disconnect(request))
    try:
        completed, _pending = await asyncio.wait(
            {conversion, disconnect}, return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        disconnect.cancel()
        conversion.cancel()
        await _drain_route_task(disconnect)
        await _discard_conversion(conversion, cancel=False)
        raise

    if disconnect in completed:
        disconnect_error: BaseException | None = None
        try:
            disconnect.result()
        except BaseException as error:
            disconnect_error = error
        cancelled = await _discard_conversion(conversion, cancel=True)
        if cancelled:
            raise asyncio.CancelledError
        if disconnect_error is not None:
            raise disconnect_error
        return None

    disconnect.cancel()
    cancelled = await _drain_route_task(disconnect)
    if cancelled:
        await _discard_conversion(conversion, cancel=False)
        raise asyncio.CancelledError
    return conversion.result()


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
            "X-SOPDS-Source-Revision": _source_revision_token(original),
        }
        return _OwnedStreamingResponse(original, headers)
    except BaseException:
        await original.stream.aclose()
        raise


@router.get("/books/{public_id}/download/{target_format}")
async def download_conversion(
    request: Request,
    public_id: str,
    target_format: str,
) -> Response:
    if target_format not in {"epub", "azw3"}:
        raise HTTPException(
            status_code=422,
            detail="Requested format is unavailable",
            headers={"Cache-Control": "no-store"},
        )
    try:
        result = await _convert_while_connected(request, public_id, target_format)
    except UnsupportedConversionError as error:
        raise HTTPException(
            status_code=422,
            detail="Requested format is unavailable",
            headers={"Cache-Control": "no-store"},
        ) from error
    except SourceUnavailableError as error:
        raise HTTPException(
            status_code=404,
            detail="Original is unavailable",
            headers={"Cache-Control": "no-store"},
        ) from error
    except ConversionShutdownError as error:
        raise HTTPException(
            status_code=503,
            detail="Service is shutting down",
            headers={"Cache-Control": "no-store"},
        ) from error
    except ConversionSourceError as error:
        _LOGGER.warning(
            f"Converted download source integrity failed surface=web phase=convert "
            f"failure_type={type(error).__name__}"
        )
        raise HTTPException(
            status_code=500,
            detail="Original cannot be converted",
            headers={"Cache-Control": "no-store"},
        ) from error
    except ConversionTimeoutError as error:
        _LOGGER.warning(
            f"Converted download timed out surface=web phase=convert "
            f"failure_type={type(error).__name__}"
        )
        raise HTTPException(
            status_code=504,
            detail="Book conversion timed out",
            headers={"Cache-Control": "no-store"},
        ) from error
    except SourceChangedError as error:
        _LOGGER.warning(
            f"Converted download source changed surface=web phase=convert "
            f"failure_type={type(error).__name__}"
        )
        raise HTTPException(
            status_code=409,
            detail="Original changed; retry the download",
            headers={"Cache-Control": "no-store"},
        ) from error
    except (ConverterExecutionError, InvalidConversionOutputError) as error:
        _LOGGER.warning(
            f"Converted download failed surface=web phase=convert "
            f"failure_type={type(error).__name__}"
        )
        raise HTTPException(
            status_code=500,
            detail="Book conversion failed",
            headers={"Cache-Control": "no-store"},
        ) from error

    if result is None:
        return _ClientDisconnectedResponse()

    try:
        headers = {
            "Content-Type": result.media_type,
            "Content-Length": str(result.content_length),
            "Content-Disposition": content_disposition(result.filename),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        }
        return _OwnedStreamingResponse(result, headers)
    except BaseException:
        await result.stream.aclose()
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
    return _localized_template_response(
        request,
        "partials/import_status.html",
        context={
            "status": status,
            "ImportState": ImportState,
            "message": message,
            **_status_presentation(request, status),
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
                message=_known_html_message(request, "No catalog changes found"),
            )
        return await _status_response(
            request,
            None,
            message=_known_html_message(request, "Manual import is starting"),
            poll=True,
            pending=True,
            poll_after_run_id=after_run_id,
        )
    if status is None and coordinator.is_import_active():
        return await _status_response(
            request,
            None,
            message=_known_html_message(request, "Catalog import is starting"),
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


def _csrf_operation_error(request: Request) -> Response:
    return _localized_template_response(
        request,
        "partials/operation_result.html",
        context={
            "message": _known_html_message(request, _CSRF_ERROR_MESSAGE),
            "error": True,
        },
        status_code=403,
        headers={"X-SOPDS-CSRF-Expired": "true"},
    )


async def _start_import(request: Request, *, force: bool) -> Response:
    try:
        _validate_csrf(request, request.headers.get("X-CSRF-Token", ""))
    except _CsrfError:
        return _csrf_operation_error(request)
    coordinator = _imports(request)
    previous_status = await coordinator.get_status()
    accepted = coordinator.start_manual_import(force=force)
    if accepted:
        message = "Force import is starting" if force else "Import check is starting"
        return await _status_response(
            request,
            None,
            status_code=202,
            message=_known_html_message(request, message),
            poll=True,
            pending=True,
            poll_after_run_id=previous_status.run_id if previous_status is not None else 0,
        )
    return await _status_response(
        request,
        await coordinator.get_status(),
        message=_known_html_message(
            request,
            "An import or database maintenance operation is already running",
        ),
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
    try:
        _validate_csrf(request, request.headers.get("X-CSRF-Token", ""))
    except _CsrfError:
        return _csrf_operation_error(request)
    vacuumed = await _imports(request).vacuum_database()
    response = _localized_template_response(
        request,
        "partials/operation_result.html",
        context={
            "message": (
                _known_html_message(request, "Database VACUUM completed")
                if vacuumed
                else _known_html_message(request, "VACUUM skipped because catalog work is running")
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
