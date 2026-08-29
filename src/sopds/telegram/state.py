"""Short-lived, chat-bound state for Telegram callbacks."""

import asyncio
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PageState:
    query: str
    cursor: str


@dataclass(frozen=True, slots=True)
class DownloadState:
    public_id: str
    target_format: str


@dataclass(frozen=True, slots=True)
class _Entry:
    chat_id: int
    state: PageState | DownloadState
    expires_at: float


class CallbackStateStore:
    """Keep bounded callback data opaque and bound to its originating chat."""

    def __init__(
        self,
        *,
        ttl_seconds: float = 15 * 60,
        max_entries: int = 1_024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_entries <= 0:
            raise ValueError("Callback state bounds must be positive")
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._lock = asyncio.Lock()

    async def put(self, chat_id: int, state: PageState) -> str:
        return await self._put(chat_id, state)

    async def get(self, token: str, chat_id: int) -> PageState | None:
        state = await self._get(token, chat_id)
        return state if isinstance(state, PageState) else None

    async def put_download(self, chat_id: int, state: DownloadState) -> str:
        return await self._put(chat_id, state)

    async def get_download(self, token: str, chat_id: int) -> DownloadState | None:
        state = await self._get(token, chat_id)
        return state if isinstance(state, DownloadState) else None

    async def _put(self, chat_id: int, state: PageState | DownloadState) -> str:
        async with self._lock:
            now = self._clock()
            self._expire(now)
            token = secrets.token_urlsafe(12)
            while token in self._entries:
                token = secrets.token_urlsafe(12)
            self._entries[token] = _Entry(chat_id, state, now + self._ttl_seconds)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
            return token

    async def _get(self, token: str, chat_id: int) -> PageState | DownloadState | None:
        async with self._lock:
            self._expire(self._clock())
            entry = self._entries.get(token)
            if entry is None or entry.chat_id != chat_id:
                return None
            self._entries.move_to_end(token)
            return entry.state

    def _expire(self, now: float) -> None:
        expired = [token for token, entry in self._entries.items() if entry.expires_at <= now]
        for token in expired:
            del self._entries[token]
