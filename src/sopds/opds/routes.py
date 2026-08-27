"""OPDS 1.2 and OpenSearch HTTP routes."""

from typing import cast
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from sopds.catalog.contracts import (
    Catalog,
    CatalogInputError,
    CatalogRequest,
    NavigationRequest,
)
from sopds.config import AppConfig
from sopds.opds.render import (
    ACQUISITION_TYPE,
    NAVIGATION_TYPE,
    OPENSEARCH_TYPE,
    acquisition_feed,
    item_entries,
    navigation_feed,
    open_search,
    query_url,
    stable_id,
)

router = APIRouter(prefix="/opds")
_XML_CHARSET = "; charset=UTF-8"


def _catalog(request: Request) -> Catalog:
    return cast(Catalog, request.app.state.catalog)


def _base_path(request: Request) -> str:
    """Keep links on the client's origin while preserving a configured proxy prefix."""
    config = cast(AppConfig, request.app.state.config)
    return (config.server.base_url.path or "").rstrip("/")


def _xml(body: bytes, media_type: str) -> Response:
    return Response(body, headers={"Content-Type": media_type + _XML_CHARSET})


def _bad_request() -> PlainTextResponse:
    return PlainTextResponse("Invalid catalog request", status_code=400)


def _common(base_path: str) -> tuple[str, str]:
    return f"{base_path}/opds/", f"{base_path}/opds/search.xml"


def _canonical_redirect(request: Request, path: str) -> RedirectResponse:
    query = request.scope.get("query_string", b"")
    suffix = b"?" + query if query else b""
    location = f"{_base_path(request)}{path}".encode("ascii") + suffix
    return RedirectResponse(location.decode("ascii"), status_code=307)


@router.get("")
async def canonical_root(request: Request) -> RedirectResponse:
    return RedirectResponse(f"{_base_path(request)}/opds/", status_code=307)


@router.get("/")
async def root(request: Request) -> Response:
    base_path = _base_path(request)
    start_url, search_url = _common(base_path)
    snapshot = await _catalog(request).snapshot()
    entries = (
        (stable_id("navigation:books"), "Books", f"{base_path}/opds/titles/", NAVIGATION_TYPE),
        (stable_id("navigation:authors"), "Authors", f"{base_path}/opds/authors/", NAVIGATION_TYPE),
        (stable_id("navigation:genres"), "Genres", f"{base_path}/opds/genres/", NAVIGATION_TYPE),
        (stable_id("navigation:series"), "Series", f"{base_path}/opds/series/", NAVIGATION_TYPE),
        (
            stable_id("navigation:languages"),
            "Languages",
            f"{base_path}/opds/languages/",
            NAVIGATION_TYPE,
        ),
    )
    body = navigation_feed(
        feed_id=stable_id("feed:root"),
        title="SOPDS",
        updated_at=snapshot.updated_at,
        self_url=start_url,
        start_url=start_url,
        up_url=None,
        search_url=search_url,
        entries=entries,
    )
    return _xml(body, NAVIGATION_TYPE)


@router.get("/books")
async def canonical_books(request: Request) -> RedirectResponse:
    return _canonical_redirect(request, "/opds/books/")


@router.get("/books/")
async def books(
    request: Request,
    q: str = "",
    author: str | None = None,
    genre: str | None = None,
    series: str | None = None,
    language: str | None = None,
    original_format: str | None = None,
    cursor: str | None = None,
) -> Response:
    base_path = _base_path(request)
    start_url, search_url = _common(base_path)
    catalog_request = CatalogRequest(
        query=q,
        author=author or None,
        genre=genre or None,
        series=series or None,
        language=language or None,
        original_format=original_format or None,
        cursor=cursor or None,
    )
    state = {
        "q": catalog_request.query or None,
        "author": catalog_request.author,
        "genre": catalog_request.genre,
        "series": catalog_request.series,
        "language": catalog_request.language,
        "original_format": catalog_request.original_format,
    }
    try:
        page = await _catalog(request).browse(catalog_request)
    except CatalogInputError:
        return _bad_request()
    self_url = query_url(base_path, "/opds/books/", {**state, "cursor": cursor})
    next_url = (
        query_url(base_path, "/opds/books/", {**state, "cursor": page.next_cursor})
        if page.next_cursor
        else None
    )
    active_origins = [
        (name, value)
        for name, value in (
            ("authors", catalog_request.author),
            ("genres", catalog_request.genre),
            ("series", catalog_request.series),
            ("languages", catalog_request.language),
        )
        if value is not None
    ]
    up_url = f"{base_path}/opds/{active_origins[0][0]}/" if len(active_origins) == 1 else start_url
    body = acquisition_feed(
        feed_id=stable_id("feed:books", state),
        title="Books",
        updated_at=page.updated_at,
        self_url=self_url,
        start_url=start_url,
        up_url=up_url,
        search_url=search_url,
        next_url=next_url,
        books=page.books,
        book_urls=tuple(
            f"{base_path}/books/{quote(book.public_id, safe='')}" for book in page.books
        ),
        download_urls=tuple(
            f"{base_path}/books/{quote(book.public_id, safe='')}/download" for book in page.books
        ),
    )
    return _xml(body, ACQUISITION_TYPE)


async def _navigation(
    request: Request,
    kind: str,
    cursor: str | None,
    *,
    prefix: str = "",
    exact: bool = False,
    parent: str | None = None,
    parent_root: bool = False,
) -> Response:
    if parent is not None and (len(parent) > 1_024 or "\x00" in parent):
        return _bad_request()
    base_path = _base_path(request)
    start_url, search_url = _common(base_path)
    try:
        page = await _catalog(request).navigation(
            NavigationRequest(kind, cursor or None, prefix, exact)
        )
    except CatalogInputError:
        return _bad_request()
    path = f"/opds/{kind}/"
    state = {
        "prefix": prefix or None,
        "exact": "1" if exact else None,
        "parent": parent,
        "parent_root": "1" if parent_root else None,
    }
    self_url = query_url(base_path, path, {**state, "cursor": cursor})
    next_url = (
        query_url(base_path, path, {**state, "cursor": page.next_cursor})
        if page.next_cursor
        else None
    )
    if parent_root:
        up_url = query_url(base_path, path, {})
    elif parent is not None:
        up_url = query_url(base_path, path, {"prefix": parent})
    else:
        up_url = start_url

    if page.grouped:
        destination_urls = tuple(
            query_url(
                base_path,
                path,
                {
                    "prefix": item.value,
                    "exact": "1" if item.exact else None,
                    "parent": prefix or None,
                    "parent_root": "1" if not prefix else None,
                },
            )
            for item in page.items
        )
        entries = item_entries(kind, page.items, destination_urls, NAVIGATION_TYPE)
        body = navigation_feed(
            feed_id=stable_id(f"feed:{kind}:prefix", [page.prefix, exact]),
            title=kind.title(),
            updated_at=page.updated_at,
            self_url=self_url,
            start_url=start_url,
            up_url=up_url,
            search_url=search_url,
            entries=entries,
            next_url=None,
        )
        return _xml(body, NAVIGATION_TYPE)

    if kind == "titles":
        body = acquisition_feed(
            feed_id=stable_id("feed:titles", [page.prefix, exact]),
            title="Books",
            updated_at=page.updated_at,
            self_url=self_url,
            start_url=start_url,
            up_url=up_url,
            search_url=search_url,
            next_url=next_url,
            books=page.books,
            book_urls=tuple(
                f"{base_path}/books/{quote(book.public_id, safe='')}" for book in page.books
            ),
            download_urls=tuple(
                f"{base_path}/books/{quote(book.public_id, safe='')}/download"
                for book in page.books
            ),
        )
        return _xml(body, ACQUISITION_TYPE)

    filter_name = {
        "authors": "author",
        "genres": "genre",
        "series": "series",
        "languages": "language",
    }[kind]
    destination_urls = tuple(
        query_url(base_path, "/opds/books/", {filter_name: item.value}) for item in page.items
    )
    entries = item_entries(kind, page.items, destination_urls)
    body = navigation_feed(
        feed_id=(
            stable_id(f"feed:{kind}", [page.prefix, exact])
            if kind in {"authors", "series"} and (page.prefix or exact)
            else stable_id(f"feed:{kind}")
        ),
        title=kind.title(),
        updated_at=page.updated_at,
        self_url=self_url,
        start_url=start_url,
        up_url=up_url,
        search_url=search_url,
        entries=entries,
        next_url=next_url,
    )
    return _xml(body, NAVIGATION_TYPE)


@router.get("/authors")
async def canonical_authors(request: Request) -> RedirectResponse:
    return _canonical_redirect(request, "/opds/authors/")


@router.get("/authors/")
async def authors(
    request: Request,
    cursor: str | None = None,
    prefix: str = "",
    exact: bool = False,
    parent: str | None = None,
    parent_root: bool = False,
) -> Response:
    return await _navigation(
        request,
        "authors",
        cursor,
        prefix=prefix,
        exact=exact,
        parent=parent,
        parent_root=parent_root,
    )


@router.get("/genres")
async def canonical_genres(request: Request) -> RedirectResponse:
    return _canonical_redirect(request, "/opds/genres/")


@router.get("/genres/")
async def genres(request: Request, cursor: str | None = None) -> Response:
    return await _navigation(request, "genres", cursor)


@router.get("/series")
async def canonical_series(request: Request) -> RedirectResponse:
    return _canonical_redirect(request, "/opds/series/")


@router.get("/series/")
async def series(
    request: Request,
    cursor: str | None = None,
    prefix: str = "",
    exact: bool = False,
    parent: str | None = None,
    parent_root: bool = False,
) -> Response:
    return await _navigation(
        request,
        "series",
        cursor,
        prefix=prefix,
        exact=exact,
        parent=parent,
        parent_root=parent_root,
    )


@router.get("/titles")
async def canonical_titles(request: Request) -> RedirectResponse:
    return _canonical_redirect(request, "/opds/titles/")


@router.get("/titles/")
async def titles(
    request: Request,
    cursor: str | None = None,
    prefix: str = "",
    exact: bool = False,
    parent: str | None = None,
    parent_root: bool = False,
) -> Response:
    return await _navigation(
        request,
        "titles",
        cursor,
        prefix=prefix,
        exact=exact,
        parent=parent,
        parent_root=parent_root,
    )


@router.get("/languages")
async def canonical_languages(request: Request) -> RedirectResponse:
    return _canonical_redirect(request, "/opds/languages/")


@router.get("/languages/")
async def languages(request: Request, cursor: str | None = None) -> Response:
    return await _navigation(request, "languages", cursor)


@router.get("/search.xml")
async def search_description(request: Request) -> Response:
    return _xml(open_search(_base_path(request)), OPENSEARCH_TYPE)
