"""Bounded, allowlisted ownership of Telegram update tasks."""

import asyncio
from collections.abc import Awaitable, Coroutine
from typing import Any, override

from telegram import Update
from telegram.ext import BaseUpdateProcessor


class TelegramUpdateProcessor(BaseUpdateProcessor):
    """Reject unauthorized work before handlers and own every admitted update task."""

    def __init__(self, allowed_chat_ids: tuple[int, ...], max_concurrent_updates: int = 4) -> None:
        super().__init__(max_concurrent_updates)
        self._allowed = frozenset(allowed_chat_ids)
        self._tasks: set[asyncio.Task[Any]] = set()
        self._accepting = True

    @override
    async def initialize(self) -> None:
        self._accepting = True

    @override
    async def shutdown(self) -> None:
        self.stop_accepting()
        await self.cancel_and_wait()

    @override
    async def do_process_update(self, update: object, coroutine: Awaitable[Any]) -> None:
        if not self._accepting or not self._is_allowed(update):
            if isinstance(coroutine, Coroutine):
                coroutine.close()
            return

        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await coroutine
        finally:
            if task is not None:
                self._tasks.discard(task)

    def stop_accepting(self) -> None:
        """Close admission without yielding so racing updates cannot reach services."""
        self._accepting = False

    async def cancel_and_wait(self) -> None:
        current = asyncio.current_task()
        tasks = [task for task in self._tasks if task is not current and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _is_allowed(self, update: object) -> bool:
        if not isinstance(update, Update):
            return False
        chat = update.effective_chat
        return chat is not None and chat.id in self._allowed
