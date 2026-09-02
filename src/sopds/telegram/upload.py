"""Cancellation-safe staging of async book streams for bounded-memory uploads."""

import asyncio
import logging
import tempfile
from collections.abc import Callable
from functools import partial
from typing import BinaryIO, Self, cast

from telegram import InputFile

from sopds.acquisition.contracts import AsyncByteStream

_LOGGER = logging.getLogger(__name__)


async def _await_blocking[T](call: Callable[[], T]) -> T:
    """Let an owned thread operation finish before propagating caller cancellation."""
    task = asyncio.create_task(asyncio.to_thread(call))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    result = await task
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _open_temporary_file() -> BinaryIO:
    task = asyncio.create_task(asyncio.to_thread(tempfile.TemporaryFile))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
    file = cast(BinaryIO, await task)
    if cancelled:
        await _await_blocking(file.close)
        raise asyncio.CancelledError
    return file


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


async def close_stream(stream: AsyncByteStream) -> None:
    """Finish caller-owned stream cleanup before propagating cancellation."""
    await _await_cleanup(asyncio.create_task(stream.aclose()))


class StagedInputFile:
    """Release the source before PTB streams an anonymous temporary file to Telegram."""

    def __init__(self, stream: AsyncByteStream, file: BinaryIO) -> None:
        self._stream = stream
        self._file = file
        self._source_close_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self.input_file: InputFile | None = None

    @classmethod
    async def create(cls, stream: AsyncByteStream, filename: str) -> Self:
        try:
            file = await _open_temporary_file()
        except BaseException as primary:
            try:
                await close_stream(stream)
            except BaseException:
                if not isinstance(primary, asyncio.CancelledError):
                    _LOGGER.warning("Telegram source cleanup failed surface=telegram phase=cleanup")
            raise
        staged = cls(stream, file)
        try:
            async for chunk in stream:
                await _await_blocking(partial(file.write, chunk))
            await staged._close_source()
            await _await_blocking(partial(file.seek, 0))
            staged.input_file = InputFile(file, filename=filename, read_file_handle=False)
            return staged
        except BaseException as primary:
            try:
                await staged.aclose()
            except BaseException:
                if not isinstance(primary, asyncio.CancelledError):
                    _LOGGER.warning(
                        "Telegram staging cleanup failed surface=telegram phase=cleanup"
                    )
            raise

    async def aclose(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await _await_cleanup(self._close_task)

    async def _close_source(self) -> None:
        if self._source_close_task is None:
            self._source_close_task = asyncio.create_task(self._stream.aclose())
        await _await_cleanup(self._source_close_task)

    async def _close(self) -> None:
        try:
            await self._close_source()
        finally:
            await _await_blocking(self._file.close)
