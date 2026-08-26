"""Failure-isolated aiogram polling lifecycle."""

import asyncio
import logging
from contextlib import suppress
from typing import Any, cast

from aiogram import Bot, Dispatcher

from sopds.acquisition.contracts import Acquisition
from sopds.catalog.contracts import Catalog
from sopds.config import TelegramConfig
from sopds.telegram.handlers import TelegramHandlers
from sopds.telegram.middleware import ActiveUpdateTracker, AllowlistMiddleware
from sopds.telegram.state import CallbackStateStore

_LOGGER = logging.getLogger(__name__)


async def _cancel_aiogram_update_tasks(dispatcher: Dispatcher) -> None:
    """Drain aiogram 3.30 tasks that may not have reached adapter middleware yet.

    Dispatcher._handle_update_tasks is private and this compatibility boundary is why
    the project pins aiogram exactly. Polling must already be joined before this runs,
    so the set cannot gain another update task while its snapshot is being cancelled.
    """
    raw_tasks = getattr(dispatcher, "_handle_update_tasks", None)
    if raw_tasks is None:
        return
    tasks = cast(set[asyncio.Task[Any]], raw_tasks)
    current = asyncio.current_task()
    pending = [task for task in tuple(tasks) if task is not current and not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


class TelegramRunner:
    """Own every Telegram resource without making HTTP startup depend on the network."""

    def __init__(
        self,
        config: TelegramConfig,
        catalog: Catalog,
        acquisition: Acquisition,
        *,
        bot: Bot | None = None,
        dispatcher: Dispatcher | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("A runner is only created for an enabled Telegram configuration")
        if config.token is None:
            raise ValueError("Enabled Telegram configuration requires a token")
        self.bot = bot or Bot(config.token.get_secret_value())
        self.dispatcher = dispatcher or Dispatcher()
        self.state = CallbackStateStore()
        self.tracker = ActiveUpdateTracker()
        self.dispatcher.update.outer_middleware(AllowlistMiddleware(config.allowed_chat_ids))
        self.dispatcher.update.outer_middleware(self.tracker)
        self.dispatcher.include_router(TelegramHandlers(catalog, acquisition, self.state).router())
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._polling_active = False
        self._webhook_deleted = False
        self._session_closed = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="telegram-polling")

    async def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set() and not self._webhook_deleted:
            try:
                await self.bot.delete_webhook(drop_pending_updates=True)
                self._webhook_deleted = True
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _LOGGER.warning("Telegram webhook setup failed: %s", type(error).__name__)
                if await self._retry_delay(delay):
                    return
                delay = min(delay * 2, 30)

        delay = 1.0
        while not self._stop.is_set() and self._webhook_deleted:
            try:
                self._polling_active = True
                await self.dispatcher.start_polling(
                    self.bot,
                    allowed_updates=["message", "callback_query"],
                    handle_signals=False,
                    close_bot_session=False,
                    handle_as_tasks=True,
                    tasks_concurrency_limit=4,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _LOGGER.warning("Telegram polling failed: %s", type(error).__name__)
            finally:
                self._polling_active = False
            if not self._stop.is_set() and await self._retry_delay(delay):
                return
            delay = min(delay * 2, 30)

    async def _retry_delay(self, delay: float) -> bool:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    async def shutdown(self) -> None:
        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(self._shutdown())
        cancelled = False
        while not self._shutdown_task.done():
            try:
                await asyncio.shield(self._shutdown_task)
            except asyncio.CancelledError:
                cancelled = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        await self._shutdown_task
        if cancelled:
            raise asyncio.CancelledError

    async def _shutdown(self) -> None:
        self.tracker.stop_accepting()
        self._stop.set()
        if self._polling_active:
            with suppress(RuntimeError):
                await self.dispatcher.stop_polling()

        task = self._task
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task

        await _cancel_aiogram_update_tasks(self.dispatcher)
        await self.tracker.cancel_and_wait()
        if not self._session_closed:
            self._session_closed = True
            await self.bot.session.close()
