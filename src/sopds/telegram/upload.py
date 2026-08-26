"""Streaming aiogram upload input with cancellation-safe, idempotent ownership."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from typing import override

from aiogram import Bot
from aiogram.types import InputFile

from sopds.acquisition.contracts import AsyncByteStream

_LOGGER = logging.getLogger(__name__)


async def _await_cleanup(task: asyncio.Task[None]) -> None:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    await task
    if cancelled:
        raise asyncio.CancelledError


class StreamingInputFile(InputFile):
    """Yield source chunks directly and close the caller-owned stream exactly once."""

    def __init__(self, stream: AsyncByteStream, filename: str) -> None:
        super().__init__(filename=filename)
        self._stream = stream
        self._close_task: asyncio.Task[None] | None = None

    @override
    async def read(self, bot: Bot) -> AsyncGenerator[bytes]:
        del bot
        primary: BaseException | None = None
        try:
            async for chunk in self._stream:
                yield chunk
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                await self.aclose()
            except BaseException:
                if primary is None:
                    raise
                _LOGGER.warning("Upload stream cleanup failed after %s", type(primary).__name__)

    async def aclose(self) -> None:
        if self._close_task is None:
            # No await occurs before publication, so every concurrent caller observes this task.
            self._close_task = asyncio.create_task(self._stream.aclose())
        await _await_cleanup(self._close_task)
