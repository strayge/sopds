"""Dispatcher-level authorization and active-update ownership."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, override

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update


def effective_chat_id(update: Update) -> int | None:
    if update.message is not None:
        return update.message.chat.id
    callback = update.callback_query
    if callback is not None and callback.message is not None:
        return callback.message.chat.id
    return None


class AllowlistMiddleware(BaseMiddleware):
    def __init__(self, allowed_chat_ids: tuple[int, ...]) -> None:
        self._allowed = frozenset(allowed_chat_ids)

    @override
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Update):
            return None
        chat_id = effective_chat_id(event)
        if chat_id is None or chat_id not in self._allowed:
            return None
        return await handler(event, data)


class ActiveUpdateTracker(BaseMiddleware):
    """Track routed update tasks so shutdown can drain them before acquisition closes."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._accepting = True

    def stop_accepting(self) -> None:
        """Close admission without yielding so a racing update cannot pass the gate."""
        self._accepting = False

    @override
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not self._accepting:
            return None
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            if task is not None:
                self._tasks.discard(task)

    async def cancel_and_wait(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
