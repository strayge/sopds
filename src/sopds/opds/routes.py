"""OPDS 1.2 and OpenSearch HTTP routes."""

from typing import cast
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from sopds.catalog.contracts import (
    Catalog,
    CatalogInputError,
    CatalogRequest,
    NavigationItem,
    NavigationRequest,
)
from sopds.config import AppConfig
from sopds.opds.render import (
    ACQUISITION_TYPE,
    NAVIGATION_TYPE,
    OPENSEARCH_TYPE,
    acquisition_feed,
    display_author_name,
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
    return f"{base_path}/opds/", f"{base_path}/opds/books/?q={{searchTerms}}"


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
        (
            stable_id("navigation:books"),
            "Books",
            None,
            f"{base_path}/opds/titles/",
            NAVIGATION_TYPE,
        ),
        (
            stable_id("navigation:authors"),
            "Authors",
            None,
            f"{base_path}/opds/authors/",
            NAVIGATION_TYPE,
        ),
        (
            stable_id("navigation:genres"),
            "Genres",
            None,
            f"{base_path}/opds/genres/",
            NAVIGATION_TYPE,
        ),
        (
            stable_id("navigation:series"),
            "Series",
            None,
            f"{base_path}/opds/series/",
            NAVIGATION_TYPE,
        ),
        (
            stable_id("navigation:languages"),
            "Languages",
            None,
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
    without_series: bool = False,
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
        without_series=without_series,
        language=language or None,
        original_format=original_format or None,
        cursor=cursor or None,
    )
    state = {
        "q": catalog_request.query or None,
        "author": catalog_request.author,
        "genre": catalog_request.genre,
        "series": catalog_request.series,
        "without_series": "1" if catalog_request.without_series else None,
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
    up_url = (
        query_url(base_path, "/opds/authors/catalog/", {"author": catalog_request.author})
        if catalog_request.author is not None
        else (
            f"{base_path}/opds/{active_origins[0][0]}/" if len(active_origins) == 1 else start_url
        )
    )
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
    author: str | None = None,
) -> Response:
    if parent is not None and (len(parent) > 1_024 or "\x00" in parent):
        return _bad_request()
    base_path = _base_path(request)
    start_url, search_url = _common(base_path)
    try:
        page = await _catalog(request).navigation(
            NavigationRequest(kind, cursor or None, prefix, exact, author)
        )
    except CatalogInputError:
        return _bad_request()
    path = f"/opds/{kind}/"
    state = {
        "prefix": prefix or None,
        "exact": "1" if exact else None,
        "parent": parent,
        "parent_root": "1" if parent_root else None,
        "author": author,
    }
    self_url = query_url(base_path, path, {**state, "cursor": cursor})
    next_url = (
        query_url(base_path, path, {**state, "cursor": page.next_cursor})
        if page.next_cursor
        else None
    )
    if parent_root:
        up_url = query_url(base_path, path, {"author": author})
    elif parent is not None:
        up_url = query_url(base_path, path, {"prefix": parent, "author": author})
    elif kind == "series" and author is not None:
        up_url = query_url(base_path, "/opds/authors/catalog/", {"author": author})
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
                    "author": author,
                },
            )
            for item in page.items
        )
        entries = item_entries(kind, page.items, destination_urls, NAVIGATION_TYPE)
        body = navigation_feed(
            feed_id=stable_id(f"feed:{kind}:prefix", [page.prefix, exact, author]),
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
    if kind == "authors":
        destination_urls = tuple(
            query_url(
                base_path,
                "/opds/authors/catalog/",
                {"author": item.value},
            )
            for item in page.items
        )
    else:
        destination_urls = tuple(
            query_url(
                base_path,
                "/opds/books/",
                {filter_name: item.value, "author": author},
            )
            for item in page.items
        )
    entries = item_entries(
        kind,
        page.items,
        destination_urls,
        NAVIGATION_TYPE if kind == "authors" else ACQUISITION_TYPE,
        count_kind="titles" if kind in {"authors", "series"} else None,
    )
    body = navigation_feed(
        feed_id=(
            stable_id(f"feed:{kind}", [page.prefix, exact, author])
            if kind in {"authors", "series"} and (page.prefix or exact or author)
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


@router.get("/authors/catalog")
async def canonical_author_catalog(request: Request) -> RedirectResponse:
    return _canonical_redirect(request, "/opds/authors/catalog/")


@router.get("/authors/catalog/")
async def author_catalog(request: Request, author: str) -> Response:
    base_path = _base_path(request)
    start_url, search_url = _common(base_path)
    try:
        counts = await _catalog(request).author_book_counts(author)
    except CatalogInputError:
        return _bad_request()
    all_books_url = query_url(base_path, "/opds/books/", {"author": author})
    if counts.series == 0:
        return RedirectResponse(all_books_url, status_code=307)

    items = [
        NavigationItem("series", "By series", counts.series, count_kind="series"),
    ]
    urls = [query_url(base_path, "/opds/series/", {"author": author})]
    media_types = [NAVIGATION_TYPE]
    if counts.without_series:
        items.append(
            NavigationItem(
                "without-series",
                "Books without series",
                counts.without_series,
                count_kind="titles",
            )
        )
        urls.append(
            query_url(
                base_path,
                "/opds/books/",
                {"author": author, "without_series": "1"},
            )
        )
        media_types.append(ACQUISITION_TYPE)
    items.append(NavigationItem("all", "All books", counts.total, count_kind="titles"))
    urls.append(all_books_url)
    media_types.append(ACQUISITION_TYPE)
    entries = tuple(
        item_entries(
            "author-catalog",
            (item,),
            (url,),
            media_type,
        )[0]
        for item, url, media_type in zip(items, urls, media_types, strict=True)
    )
    self_url = query_url(base_path, "/opds/authors/catalog/", {"author": author})
    body = navigation_feed(
        feed_id=stable_id("feed:author-catalog", author),
        title=f"Books by {display_author_name(author)}",
        updated_at=counts.updated_at,
        self_url=self_url,
        start_url=start_url,
        up_url=f"{base_path}/opds/authors/",
        search_url=search_url,
        entries=entries,
    )
    return _xml(body, NAVIGATION_TYPE)


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
    author: str | None = None,
) -> Response:
    return await _navigation(
        request,
        "series",
        cursor,
        prefix=prefix,
        exact=exact,
        parent=parent,
        parent_root=parent_root,
        author=author,
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
