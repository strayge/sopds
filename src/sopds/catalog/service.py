"""Catalog request validation, cursor handling, and query orchestration."""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC

from sopds.catalog.contracts import (
    BookDetail,
    CatalogFilters,
    CatalogInputError,
    CatalogPage,
    CatalogRequest,
    CatalogSnapshot,
    CatalogStaleCursorError,
    CatalogStatistics,
    NavigationItem,
    NavigationPage,
    NavigationRequest,
    SearchField,
)
from sopds.catalog.search import fts_match_expression, normalize_text, query_tokens
from sopds.db.repository import CatalogRepository

PAGE_SIZE = 50
MIN_PAGE_SIZE = 1
MAX_PAGE_SIZE = 50
MAX_CURSOR_CHARS = 2_048
MAX_FILTER_CHARS = 128
MAX_NAME_FILTER_CHARS = 512
MAX_PREFIX_CHARS = 1_024
NAVIGATION_GROUP_THRESHOLD = 100
_CURSOR_SIGNATURE_BYTES = 32


@dataclass(frozen=True, slots=True)
class _Cursor:
    generation_id: int
    title_sort: str
    public_id: str


class CatalogService:
    def __init__(self, repository: CatalogRepository, cursor_key: bytes) -> None:
        if not cursor_key:
            raise ValueError("Cursor key must not be empty")
        self._repository = repository
        self._cursor_key = cursor_key
        self._filters_cache: tuple[CatalogSnapshot, CatalogFilters] | None = None
        self._filters_lock = asyncio.Lock()
        self._filters_revision = 0

    async def check_readiness(self) -> None:
        await self._repository.check_readiness()

    async def browse(self, request: CatalogRequest) -> CatalogPage:
        _validate_filters(request)
        tokens = query_tokens(request.query)
        normalized = " ".join(tokens)
        fingerprint = _request_fingerprint(request, normalized)
        match = fts_match_expression(tokens, request.search_field)

        for attempt in range(2):
            snapshot = await self._repository.active_snapshot()
            generation_id = snapshot.generation_id
            if generation_id is None:
                if request.cursor is not None:
                    raise CatalogStaleCursorError("Catalog cursor is stale")
                return CatalogPage(books=(), next_cursor=None, updated_at=snapshot.updated_at)
            cursor = _decode_cursor(request.cursor, snapshot, fingerprint, self._cursor_key)
            after = None if cursor is None else (cursor.title_sort, cursor.public_id)
            if match is None:
                rows = await self._repository.browse_book_ids(
                    generation_id,
                    language=request.language,
                    genre=request.genre,
                    original_format=request.original_format,
                    author=request.author,
                    series=request.series,
                    after=after,
                    limit=request.page_size + 1,
                )
            else:
                rows = await self._repository.search_book_ids(
                    generation_id,
                    match,
                    language=request.language,
                    genre=request.genre,
                    original_format=request.original_format,
                    author=request.author,
                    series=request.series,
                    after=after,
                    limit=request.page_size + 1,
                )
            visible = rows[: request.page_size]
            books = tuple(
                await self._repository.summaries(generation_id, [row[0] for row in visible])
            )
            if await self._repository.active_snapshot() != snapshot:
                if request.cursor is not None:
                    raise CatalogStaleCursorError("Catalog cursor is stale")
                if attempt == 0:
                    continue
                raise CatalogInputError("Catalog changed while loading; retry the request")

            next_cursor = None
            if len(rows) > request.page_size and visible and len(books) == len(visible):
                last = visible[-1]
                next_cursor = _encode_cursor(
                    snapshot,
                    last[1],
                    last[2],
                    fingerprint,
                    self._cursor_key,
                )
            return CatalogPage(books=books, next_cursor=next_cursor, updated_at=snapshot.updated_at)

        raise AssertionError("Catalog browse retry bound was bypassed")

    async def snapshot(self) -> CatalogSnapshot:
        return await self._repository.active_snapshot()

    async def statistics(self) -> CatalogStatistics:
        for attempt in range(2):
            snapshot = await self._repository.active_snapshot()
            statistics = await self._repository.catalog_statistics(snapshot.generation_id)
            if await self._repository.active_snapshot() == snapshot:
                return statistics
            if attempt == 1:
                raise CatalogInputError("Catalog changed while loading; retry the request")
        raise AssertionError("Catalog statistics retry bound was bypassed")

    async def navigation(self, request: NavigationRequest) -> NavigationPage:
        if request.kind not in {"authors", "genres", "series", "languages", "titles"}:
            raise CatalogInputError("Invalid navigation kind")
        if request.kind in {"authors", "series", "titles"}:
            return await self._adaptive_navigation(request)
        if request.prefix or request.exact:
            raise CatalogInputError("Prefixes are not supported for this navigation kind")

        fingerprint = f"navigation:{request.kind}"
        for attempt in range(2):
            snapshot = await self._repository.active_snapshot()
            generation_id = snapshot.generation_id
            if generation_id is None:
                if request.cursor is not None:
                    raise CatalogStaleCursorError("Catalog cursor is stale")
                return NavigationPage((), None, snapshot.updated_at)
            cursor = _decode_cursor(request.cursor, snapshot, fingerprint, self._cursor_key)
            after = None if cursor is None else (cursor.title_sort, cursor.public_id)
            rows = await self._repository.navigation_items(
                generation_id, request.kind, after=after, limit=PAGE_SIZE + 1
            )
            visible = rows[:PAGE_SIZE]
            if await self._repository.active_snapshot() != snapshot:
                if request.cursor is not None:
                    raise CatalogStaleCursorError("Catalog cursor is stale")
                if attempt == 0:
                    continue
                raise CatalogInputError("Catalog changed while loading; retry the request")
            next_cursor = None
            if len(rows) > PAGE_SIZE and visible:
                last = visible[-1]
                next_cursor = _encode_cursor(
                    snapshot, last[1], last[0], fingerprint, self._cursor_key
                )
            return NavigationPage(
                tuple(NavigationItem(value=row[2], label=row[3]) for row in visible),
                next_cursor,
                snapshot.updated_at,
            )
        raise AssertionError("Catalog navigation retry bound was bypassed")

    async def _adaptive_navigation(self, request: NavigationRequest) -> NavigationPage:
        if (
            not isinstance(request.prefix, str)
            or len(request.prefix) > MAX_PREFIX_CHARS
            or "\x00" in request.prefix
            or type(request.exact) is not bool
        ):
            raise CatalogInputError("Invalid navigation prefix")
        requested_prefix = normalize_text(request.prefix)
        if len(requested_prefix) > MAX_PREFIX_CHARS:
            raise CatalogInputError("Invalid navigation prefix")
        fingerprint = f"navigation:{request.kind}:{requested_prefix}:{int(request.exact)}"

        for attempt in range(2):
            snapshot = await self._repository.active_snapshot()
            generation_id = snapshot.generation_id
            if generation_id is None:
                if request.cursor is not None:
                    raise CatalogStaleCursorError("Catalog cursor is stale")
                return NavigationPage((), None, snapshot.updated_at, prefix=requested_prefix)

            cursor = _decode_cursor(request.cursor, snapshot, fingerprint, self._cursor_key)
            prefix = requested_prefix
            grouped_items: tuple[NavigationItem, ...] | None = None
            if not request.exact:
                while True:
                    buckets = await self._repository.navigation_prefix_buckets(
                        generation_id, request.kind, prefix
                    )
                    total = sum(count for _, count in buckets)
                    if total <= NAVIGATION_GROUP_THRESHOLD:
                        break
                    terminal_count = next(
                        (count for character, count in buckets if not character), 0
                    )
                    children = [(character, count) for character, count in buckets if character]
                    if terminal_count and not children:
                        break
                    if terminal_count == 0 and len(children) == 1:
                        prefix += children[0][0]
                        continue
                    group_items: list[NavigationItem] = []
                    if terminal_count:
                        group_items.append(
                            NavigationItem(
                                prefix,
                                f"{prefix.capitalize()} (exact) ({terminal_count})",
                                terminal_count,
                                exact=True,
                            )
                        )
                    group_items.extend(
                        NavigationItem(
                            prefix + character,
                            f"{(prefix + character).capitalize()}… ({count})",
                            count,
                        )
                        for character, count in children
                    )
                    grouped_items = tuple(group_items)
                    break

            if grouped_items is not None:
                if cursor is not None:
                    raise CatalogInputError("Grouped navigation does not use a cursor")
                if await self._repository.active_snapshot() != snapshot:
                    if attempt == 0:
                        continue
                    raise CatalogInputError("Catalog changed while loading; retry the request")
                return NavigationPage(
                    grouped_items,
                    None,
                    snapshot.updated_at,
                    prefix=prefix,
                    grouped=True,
                )

            after = None if cursor is None else (cursor.title_sort, cursor.public_id)
            rows = await self._repository.navigation_prefix_items(
                generation_id,
                request.kind,
                prefix,
                exact=request.exact,
                after=after,
                limit=PAGE_SIZE + 1,
            )
            visible = rows[:PAGE_SIZE]
            books = (
                tuple(await self._repository.summaries(generation_id, [row[0] for row in visible]))
                if request.kind == "titles"
                else ()
            )
            if await self._repository.active_snapshot() != snapshot:
                if request.cursor is not None:
                    raise CatalogStaleCursorError("Catalog cursor is stale")
                if attempt == 0:
                    continue
                raise CatalogInputError("Catalog changed while loading; retry the request")
            next_cursor = None
            if (
                len(rows) > PAGE_SIZE
                and visible
                and (request.kind != "titles" or len(books) == len(visible))
            ):
                last = visible[-1]
                next_cursor = _encode_cursor(
                    snapshot, last[1], last[2], fingerprint, self._cursor_key
                )
            leaf_items = (
                ()
                if request.kind == "titles"
                else tuple(NavigationItem(value=row[3], label=row[4]) for row in visible)
            )
            return NavigationPage(
                leaf_items,
                next_cursor,
                snapshot.updated_at,
                prefix=prefix,
                books=books,
            )

        raise AssertionError("Adaptive navigation retry bound was bypassed")

    async def details(self, public_id: str) -> BookDetail | None:
        if not public_id or len(public_id) > 64:
            return None
        for attempt in range(2):
            snapshot = await self._repository.active_snapshot()
            generation_id = snapshot.generation_id
            if generation_id is None:
                return None
            detail = await self._repository.detail(generation_id, public_id)
            if await self._repository.active_snapshot() != snapshot:
                if attempt == 0:
                    continue
                raise CatalogInputError("Catalog changed while loading; retry the request")
            return detail

        raise AssertionError("Catalog detail retry bound was bypassed")

    async def filters(self) -> CatalogFilters:
        snapshot = await self._repository.active_snapshot()
        cached = self._filters_cache
        if cached is not None and cached[0] == snapshot:
            return cached[1]

        async with self._filters_lock:
            for attempt in range(2):
                snapshot = await self._repository.active_snapshot()
                cached = self._filters_cache
                if cached is not None and cached[0] == snapshot:
                    return cached[1]
                if snapshot.generation_id is None:
                    return CatalogFilters(languages=(), genres=(), original_formats=())

                revision = self._filters_revision
                filters = await self._repository.catalog_filters(snapshot.generation_id)
                active_snapshot = await self._repository.active_snapshot()
                if active_snapshot != snapshot or revision != self._filters_revision:
                    if attempt == 0:
                        continue
                    raise CatalogInputError("Catalog changed while loading; retry the request")

                self._filters_cache = (snapshot, filters)
                return filters

        raise AssertionError("Catalog filter retry bound was bypassed")

    def invalidate_filters(self) -> None:
        """Discard facets when availability can change inside the active generation."""
        self._filters_revision += 1
        self._filters_cache = None


def _validate_filters(request: CatalogRequest) -> None:
    if not isinstance(request.search_field, SearchField):
        raise CatalogInputError("Invalid search field")
    if (
        type(request.page_size) is not int
        or not MIN_PAGE_SIZE <= request.page_size <= MAX_PAGE_SIZE
    ):
        raise CatalogInputError("Invalid catalog page size")
    for value in (request.language, request.genre, request.original_format):
        if value is not None and (not value or len(value) > MAX_FILTER_CHARS or "\x00" in value):
            raise CatalogInputError("Invalid catalog filter")
    for value in (request.author, request.series):
        if value is not None and (
            not value or len(value) > MAX_NAME_FILTER_CHARS or "\x00" in value
        ):
            raise CatalogInputError("Invalid catalog filter")


def _request_fingerprint(request: CatalogRequest, normalized: str) -> str:
    payload = json.dumps(
        [
            normalized,
            request.language,
            request.genre,
            request.original_format,
            request.author,
            request.series,
            request.search_field.value,
            request.page_size,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:24]


def _snapshot_revision(snapshot: CatalogSnapshot) -> str:
    value = snapshot.updated_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _encode_cursor(
    snapshot: CatalogSnapshot,
    title_sort: str,
    public_id: str,
    fingerprint: str,
    key: bytes,
) -> str:
    if snapshot.generation_id is None:
        raise AssertionError("A cursor requires an active generation")
    payload = json.dumps(
        {
            "v": 2,
            "g": snapshot.generation_id,
            "r": _snapshot_revision(snapshot),
            "t": title_sort,
            "p": public_id,
            "f": fingerprint,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    signature = hmac.digest(key, payload, "sha256")
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode()


def _decode_cursor(
    value: str | None,
    snapshot: CatalogSnapshot,
    fingerprint: str,
    key: bytes,
) -> _Cursor | None:
    if value is None:
        return None
    if not value or len(value) > MAX_CURSOR_CHARS:
        raise CatalogInputError("Invalid catalog cursor")
    try:
        padding = "=" * (-len(value) % 4)
        raw = base64.b64decode(value + padding, altchars=b"-_", validate=True)
        if len(raw) <= _CURSOR_SIGNATURE_BYTES:
            raise ValueError
        payload = raw[:-_CURSOR_SIGNATURE_BYTES]
        signature = raw[-_CURSOR_SIGNATURE_BYTES:]
        expected_signature = hmac.digest(key, payload, "sha256")
        if not hmac.compare_digest(signature, expected_signature):
            raise CatalogInputError("Invalid catalog cursor")
        decoded = json.loads(payload)
        if not isinstance(decoded, dict) or set(decoded) != {"v", "g", "r", "t", "p", "f"}:
            raise ValueError
        version = decoded["v"]
        cursor_generation = decoded["g"]
        cursor_revision = decoded["r"]
        cursor_fingerprint = decoded["f"]
        if type(version) is not int or type(cursor_generation) is not int:
            raise ValueError
        if (
            version != 2
            or cursor_generation != snapshot.generation_id
            or not isinstance(cursor_revision, str)
            or cursor_revision != _snapshot_revision(snapshot)
        ):
            raise CatalogStaleCursorError("Catalog cursor is stale")
        if not isinstance(cursor_fingerprint, str) or cursor_fingerprint != fingerprint:
            raise CatalogInputError("Catalog cursor does not match this query")
        title_sort = decoded["t"]
        public_id = decoded["p"]
        if not isinstance(title_sort, str) or not isinstance(public_id, str):
            raise ValueError
        if len(title_sort) > 1_024 or not public_id or len(public_id) > 64:
            raise ValueError
        return _Cursor(cursor_generation, title_sort, public_id)
    except CatalogInputError:
        raise
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
    ) as error:
        raise CatalogInputError("Invalid catalog cursor") from error
