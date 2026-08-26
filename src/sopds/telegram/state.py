"""Short-lived, chat-bound state for Telegram pagination callbacks."""

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
class _Entry:
    chat_id: int
    state: PageState
    expires_at: float


class CallbackStateStore:
    """Keep catalog cursors out of callback data and bound to their originating chat."""

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

    async def get(self, token: str, chat_id: int) -> PageState | None:
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
