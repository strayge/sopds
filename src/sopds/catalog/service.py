"""Catalog request validation, cursor handling, and query orchestration."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
from dataclasses import dataclass

from sopds.catalog.contracts import (
    BookDetail,
    CatalogFilters,
    CatalogInputError,
    CatalogPage,
    CatalogRequest,
    CatalogStaleCursorError,
)
from sopds.catalog.search import fts_match_expression, query_tokens
from sopds.db.repository import CatalogRepository

PAGE_SIZE = 50
MAX_CURSOR_CHARS = 2_048
MAX_FILTER_CHARS = 128
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
        self._filters_cache: tuple[int, CatalogFilters] | None = None
        self._filters_lock = asyncio.Lock()
        self._filters_revision = 0

    async def browse(self, request: CatalogRequest) -> CatalogPage:
        _validate_filters(request)
        tokens = query_tokens(request.query)
        normalized = " ".join(tokens)
        fingerprint = _request_fingerprint(request, normalized)
        match = fts_match_expression(tokens)

        for attempt in range(2):
            generation_id = await self._repository.active_generation_id()
            if generation_id is None:
                if request.cursor is not None:
                    raise CatalogStaleCursorError("Catalog cursor is stale")
                return CatalogPage(books=(), next_cursor=None)
            cursor = _decode_cursor(request.cursor, generation_id, fingerprint, self._cursor_key)
            after = None if cursor is None else (cursor.title_sort, cursor.public_id)
            if match is None:
                rows = await self._repository.browse_book_ids(
                    generation_id,
                    language=request.language,
                    genre=request.genre,
                    original_format=request.original_format,
                    after=after,
                    limit=PAGE_SIZE + 1,
                )
            else:
                rows = await self._repository.search_book_ids(
                    generation_id,
                    match,
                    language=request.language,
                    genre=request.genre,
                    original_format=request.original_format,
                    after=after,
                    limit=PAGE_SIZE + 1,
                )
            visible = rows[:PAGE_SIZE]
            books = tuple(
                await self._repository.summaries(generation_id, [row[0] for row in visible])
            )
            if await self._repository.active_generation_id() != generation_id:
                if request.cursor is not None:
                    raise CatalogStaleCursorError("Catalog cursor is stale")
                if attempt == 0:
                    continue
                raise CatalogInputError("Catalog changed while loading; retry the request")

            next_cursor = None
            if len(rows) > PAGE_SIZE and visible and len(books) == len(visible):
                last = visible[-1]
                next_cursor = _encode_cursor(
                    generation_id,
                    last[1],
                    last[2],
                    fingerprint,
                    self._cursor_key,
                )
            return CatalogPage(books=books, next_cursor=next_cursor)

        raise AssertionError("Catalog browse retry bound was bypassed")

    async def details(self, public_id: str) -> BookDetail | None:
        if not public_id or len(public_id) > 64:
            return None
        for attempt in range(2):
            generation_id = await self._repository.active_generation_id()
            if generation_id is None:
                return None
            detail = await self._repository.detail(generation_id, public_id)
            if await self._repository.active_generation_id() != generation_id:
                if attempt == 0:
                    continue
                raise CatalogInputError("Catalog changed while loading; retry the request")
            return detail

        raise AssertionError("Catalog detail retry bound was bypassed")

    async def filters(self) -> CatalogFilters:
        generation_id = await self._repository.active_generation_id()
        cached = self._filters_cache
        if cached is not None and cached[0] == generation_id:
            return cached[1]

        async with self._filters_lock:
            for attempt in range(2):
                generation_id = await self._repository.active_generation_id()
                cached = self._filters_cache
                if cached is not None and cached[0] == generation_id:
                    return cached[1]
                if generation_id is None:
                    return CatalogFilters(languages=(), genres=(), original_formats=())

                revision = self._filters_revision
                filters = await self._repository.catalog_filters(generation_id)
                active_generation_id = await self._repository.active_generation_id()
                if active_generation_id != generation_id or revision != self._filters_revision:
                    if attempt == 0:
                        continue
                    raise CatalogInputError("Catalog changed while loading; retry the request")

                self._filters_cache = (generation_id, filters)
                return filters

        raise AssertionError("Catalog filter retry bound was bypassed")

    def invalidate_filters(self) -> None:
        """Discard facets when availability can change inside the active generation."""
        self._filters_revision += 1
        self._filters_cache = None


def _validate_filters(request: CatalogRequest) -> None:
    for value in (request.language, request.genre, request.original_format):
        if value is not None and (not value or len(value) > MAX_FILTER_CHARS or "\x00" in value):
            raise CatalogInputError("Invalid catalog filter")


def _request_fingerprint(request: CatalogRequest, normalized: str) -> str:
    payload = json.dumps(
        [normalized, request.language, request.genre, request.original_format],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:24]


def _encode_cursor(
    generation_id: int,
    title_sort: str,
    public_id: str,
    fingerprint: str,
    key: bytes,
) -> str:
    payload = json.dumps(
        {"v": 1, "g": generation_id, "t": title_sort, "p": public_id, "f": fingerprint},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    signature = hmac.digest(key, payload, "sha256")
    return base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode()


def _decode_cursor(
    value: str | None,
    generation_id: int,
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
        if not isinstance(decoded, dict) or set(decoded) != {"v", "g", "t", "p", "f"}:
            raise ValueError
        version = decoded["v"]
        cursor_generation = decoded["g"]
        cursor_fingerprint = decoded["f"]
        if type(version) is not int or type(cursor_generation) is not int:
            raise ValueError
        if version != 1 or cursor_generation != generation_id:
            raise CatalogStaleCursorError("Catalog cursor is stale")
        if not isinstance(cursor_fingerprint, str) or cursor_fingerprint != fingerprint:
            raise CatalogInputError("Catalog cursor does not match this query")
        title_sort = decoded["t"]
        public_id = decoded["p"]
        if not isinstance(title_sort, str) or not isinstance(public_id, str):
            raise ValueError
        if len(title_sort) > 1_024 or not public_id or len(public_id) > 64:
            raise ValueError
        return _Cursor(generation_id, title_sort, public_id)
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
