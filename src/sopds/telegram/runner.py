"""Failure-isolated python-telegram-bot polling lifecycle."""

import asyncio
import logging
from contextlib import suppress
from typing import Any, cast

from telegram.error import TelegramError
from telegram.ext import Application

from sopds.acquisition.contracts import Acquisition
from sopds.catalog.contracts import Catalog
from sopds.config import TelegramConfig
from sopds.conversion.policy import OUTPUT_POLICY, OutputPolicy
from sopds.conversion.service import ConversionService
from sopds.telegram.handlers import TelegramHandlers
from sopds.telegram.middleware import TelegramUpdateProcessor
from sopds.telegram.state import CallbackStateStore

_LOGGER = logging.getLogger(__name__)
_PTB_POLLING_TASK_ATTRIBUTE = "_Updater__polling_task"
type TelegramApplication = Application[Any, Any, Any, Any, Any, Any]


class TelegramRunner:
    """Own PTB resources without making HTTP startup depend on Telegram availability."""

    def __init__(
        self,
        config: TelegramConfig,
        catalog: Catalog,
        acquisition: Acquisition,
        conversion: ConversionService | None = None,
        output_policy: OutputPolicy = OUTPUT_POLICY,
        *,
        application: TelegramApplication | None = None,
        processor: TelegramUpdateProcessor | None = None,
    ) -> None:
        if not config.enabled:
            raise ValueError("A runner is only created for an enabled Telegram configuration")
        if config.token is None:
            raise ValueError("Enabled Telegram configuration requires a token")

        self.state = CallbackStateStore()
        self.processor = processor or TelegramUpdateProcessor(config.allowed_chat_ids)
        if application is None:
            self.application = cast(
                TelegramApplication,
                Application.builder()
                .token(config.token.get_secret_value())
                .concurrent_updates(self.processor)
                .media_write_timeout(60)
                .build(),
            )
        else:
            if processor is None:
                raise ValueError("An injected Telegram application requires its update processor")
            self.application = application

        updater = self.application.updater
        if updater is None:
            raise ValueError("Telegram polling requires an Application with an Updater")
        self.updater = updater
        TelegramHandlers(
            catalog,
            acquisition,
            self.state,
            conversion,
            output_policy,
        ).register(self.application)

        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._shutdown_task: asyncio.Task[None] | None = None
        self._application_initialized = False
        self._application_running = False
        self._polling_running = False
        self._pending_updates_dropped = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="telegram-polling")

    def polling_task_running(self) -> bool | None:
        """Distinguish a runner that was never started from one that stopped."""
        return None if self._task is None else not self._task.done()

    async def _run(self) -> None:
        delay = 1.0
        failures = 0
        try:
            while not self._stop.is_set():
                try:
                    await self._start_application()
                    if failures:
                        _LOGGER.info(
                            f"Telegram polling recovered phase=polling failure_count={failures}"
                        )
                        failures = 0
                        delay = 1.0
                    _LOGGER.info("Telegram polling started phase=polling")
                    await self._wait_for_stop_or_polling_failure()
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    failures += 1
                    _LOGGER.warning(
                        f"Telegram polling failed phase=polling "
                        f"failure_type={type(error).__name__} attempt={failures} "
                        f"retry_delay_seconds={delay}"
                    )
                    await self._cleanup_after_failed_start()
                    if self._stop.is_set() or await self._retry_delay(delay):
                        return
                    delay = min(delay * 2, 30)
        finally:
            await self._stop_application()
            _LOGGER.info("Telegram polling stopped phase=polling")

    async def _start_application(self) -> None:
        await self.application.initialize()
        self._application_initialized = True
        await self.updater.start_polling(
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=not self._pending_updates_dropped,
            bootstrap_retries=0,
            error_callback=self._polling_error,
        )
        self._pending_updates_dropped = True
        self._polling_running = True
        await self.application.start()
        self._application_running = True

    async def _wait_for_stop_or_polling_failure(self) -> None:
        """Observe PTB's private task because its public start call returns only the queue."""
        polling_task = getattr(self.updater, _PTB_POLLING_TASK_ATTRIBUTE, None)
        if not isinstance(polling_task, asyncio.Task):
            raise RuntimeError("PTB polling task is unavailable")
        stop_task = asyncio.create_task(self._stop.wait())
        try:
            done, _pending = await asyncio.wait(
                (polling_task, stop_task), return_when=asyncio.FIRST_COMPLETED
            )
            if stop_task in done:
                return
            await polling_task
            raise RuntimeError("PTB polling stopped unexpectedly")
        finally:
            if not stop_task.done():
                stop_task.cancel()
            with suppress(asyncio.CancelledError):
                await stop_task

    def _polling_error(self, error: TelegramError) -> None:
        _LOGGER.warning(
            f"Telegram polling request failed phase=polling failure_type={type(error).__name__}"
        )

    async def _cleanup_after_failed_start(self) -> None:
        try:
            await self._stop_application()
        except Exception as error:
            _LOGGER.warning(
                f"Telegram failed-start cleanup failed phase=cleanup "
                f"failure_type={type(error).__name__}"
            )

    async def _stop_application(self) -> None:
        self.processor.stop_accepting()
        failures: list[BaseException] = []

        if self._polling_running or self.updater.running:
            try:
                await self.updater.stop()
            except BaseException as error:
                failures.append(error)
            finally:
                self._polling_running = False

        try:
            await self.processor.cancel_and_wait()
        except BaseException as error:
            failures.append(error)

        if self._application_running or self.application.running:
            try:
                await self.application.stop()
            except BaseException as error:
                failures.append(error)
            finally:
                self._application_running = False

        if self._application_initialized:
            try:
                await self.application.shutdown()
            except BaseException as error:
                failures.append(error)
            finally:
                self._application_initialized = False
        else:
            try:
                await self.application.bot.shutdown()
            except BaseException as error:
                failures.append(error)

        if failures:
            raise failures[0]

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
        self.processor.stop_accepting()
        self._stop.set()
        task = self._task
        if task is not None:
            if not task.done():
                task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        else:
            await self._stop_application()
        _LOGGER.info("Telegram shutdown completed phase=shutdown")
