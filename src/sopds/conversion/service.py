"""Conversion orchestration across acquisition, registry, and artifact cache."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from functools import partial
from pathlib import Path
from typing import BinaryIO

from sopds.acquisition.contracts import Acquisition, AcquisitionError, AsyncByteStream
from sopds.acquisition.service import safe_download_filename
from sopds.conversion.cache import CACHE_CHUNK_SIZE, ArtifactCache, cache_digest
from sopds.conversion.contracts import (
    ConversionError,
    ConversionResult,
    ConversionSourceKey,
    ConversionTimeoutError,
    ConverterExecutionError,
    SourceChangedError,
    SourceUnavailableError,
    UnsupportedConversionError,
    normalize_format,
)
from sopds.conversion.registry import ConverterRegistry, RegisteredCapability


async def _blocking[T](
    function: Callable[[], T], *, cancel_cleanup: Callable[[T], None] | None = None
) -> T:
    """Drain an in-flight thread call before propagating caller cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        exception = task.exception()
        if exception is None and cancel_cleanup is not None:
            cancel_cleanup(task.result())
        raise


class ConversionService:
    """Use the request's described source snapshot for cache hits and misses."""

    def __init__(
        self,
        acquisition: Acquisition,
        registry: ConverterRegistry,
        cache: ArtifactCache,
    ) -> None:
        self._acquisition = acquisition
        self._registry = registry
        self._cache = cache

    async def convert(self, public_id: str, target_format: str) -> ConversionResult:
        try:
            description = await self._acquisition.describe(public_id)
        except AcquisitionError as error:
            raise SourceUnavailableError("Conversion source is unavailable") from error
        try:
            registration = self._registry.resolve(description.source_format, target_format)
        except ValueError:
            raise UnsupportedConversionError("Requested conversion is unsupported") from None

        key = ConversionSourceKey(
            public_id=description.public_id,
            revision=description.revision,
            source_format=description.source_format,
            target_format=registration.capability.target_format,
            converter=registration.converter.identity,
        )

        async def produce(output_path: Path) -> None:
            await self._produce(key, registration, output_path)

        artifact = await self._cache.get_or_create(key, produce)
        return ConversionResult(
            filename=safe_download_filename(
                description.title, registration.capability.target_extension
            ),
            media_type=registration.capability.target_media_type,
            content_length=artifact.content_length,
            stream=artifact.stream,
        )

    async def _produce(
        self,
        key: ConversionSourceKey,
        registration: RegisteredCapability,
        output_path: Path,
    ) -> None:
        try:
            original = await self._acquisition.acquire(key.public_id)
        except AcquisitionError as error:
            raise SourceUnavailableError("Conversion source is unavailable") from error
        try:
            try:
                observed_format = normalize_format(original.source_format)
            except ValueError:
                observed_format = ""
            if original.source_revision != key.revision or observed_format != key.source_format:
                raise SourceChangedError("Conversion source changed during acquisition")

            source_path = await self._cache.create_source_path(cache_digest(key))
            try:
                try:
                    await self._spool(original.stream, source_path)
                except AcquisitionError as error:
                    raise SourceUnavailableError("Conversion source is unavailable") from error
                try:
                    await registration.converter.convert(
                        source_path, registration.capability.target_format, output_path
                    )
                except TimeoutError as error:
                    raise ConversionTimeoutError("Converter timed out") from error
                except ConversionError, asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise ConverterExecutionError("Converter execution failed") from error
            finally:
                await self._cache.remove_source_path(source_path)
        finally:
            with suppress(Exception):
                await original.stream.aclose()

    async def _spool(self, stream: AsyncByteStream, path: Path) -> None:
        file: BinaryIO = await _blocking(
            lambda: path.open("wb"), cancel_cleanup=lambda opened: opened.close()
        )
        try:
            async for chunk in stream:
                for offset in range(0, len(chunk), CACHE_CHUNK_SIZE):
                    part = chunk[offset : offset + CACHE_CHUNK_SIZE]
                    written = await _blocking(partial(file.write, part))
                    if written != len(part):
                        raise OSError("short temporary source write")
            await _blocking(file.flush)
        except OSError as error:
            raise SourceUnavailableError("Conversion source is unavailable") from error
        finally:
            await _blocking(file.close)

    async def shutdown(self) -> None:
        await self._cache.shutdown()
