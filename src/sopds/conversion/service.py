"""Conversion orchestration across acquisition, registry, and artifact cache."""

import asyncio
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from functools import partial
from pathlib import Path
from typing import BinaryIO, Never

from sopds.acquisition.contracts import (
    Acquisition,
    AcquisitionError,
    AcquisitionMemberNotFoundError,
    AcquisitionNotFoundError,
    AcquisitionUnavailableError,
    AsyncByteStream,
)
from sopds.acquisition.service import safe_download_filename
from sopds.conversion.cache import CACHE_CHUNK_SIZE, ArtifactCache, cache_digest
from sopds.conversion.contracts import (
    ConversionError,
    ConversionResult,
    ConversionSourceError,
    ConversionSourceKey,
    ConversionTimeoutError,
    ConverterExecutionError,
    SourceChangedError,
    SourceUnavailableError,
    UnsupportedConversionError,
    normalize_format,
)
from sopds.conversion.registry import ConverterRegistry, RegisteredCapability


class _ProcessWideConversionLimiter:
    """Share permits across event loops without binding admission state to any loop."""

    def __init__(self, capacity: int) -> None:
        self._semaphore = threading.BoundedSemaphore(capacity)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        while not self._semaphore.acquire(blocking=False):  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        try:
            yield
        finally:
            self._semaphore.release()


_CONVERSION_SLOTS = _ProcessWideConversionLimiter(2)
_UNAVAILABLE_ERRORS = (
    AcquisitionNotFoundError,
    AcquisitionUnavailableError,
    AcquisitionMemberNotFoundError,
)


def _raise_source_error(error: AcquisitionError) -> Never:
    if isinstance(error, _UNAVAILABLE_ERRORS):
        raise SourceUnavailableError("Conversion source is unavailable") from error
    raise ConversionSourceError("Conversion source failed integrity checks") from error


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
            with suppress(BaseException):
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

    def supports(self, source_format: str, target_format: str) -> bool:
        """Report only capabilities backed by a registered converter."""
        return self._registry.supports(source_format, target_format)

    async def convert(
        self,
        public_id: str,
        target_format: str,
        *,
        expected_generation_id: int | None = None,
    ) -> ConversionResult:
        try:
            description = await self._acquisition.describe(
                public_id, expected_generation_id=expected_generation_id
            )
        except AcquisitionError as error:
            _raise_source_error(error)
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
            await self._produce(
                key,
                registration,
                output_path,
                expected_generation_id=expected_generation_id,
            )

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
        *,
        expected_generation_id: int | None,
    ) -> None:
        try:
            original = await self._acquisition.acquire(
                key.public_id, expected_generation_id=expected_generation_id
            )
        except AcquisitionError as error:
            _raise_source_error(error)
        try:
            try:
                observed_format = normalize_format(original.source_format)
            except ValueError:
                observed_format = ""
            if original.source_revision != key.revision or observed_format != key.source_format:
                raise SourceChangedError("Conversion source changed during acquisition")

            try:
                source_path = await self._cache.create_source_path(
                    cache_digest(key), key.source_format
                )
            except OSError as error:
                raise ConversionSourceError("Conversion source failed integrity checks") from error
            try:
                try:
                    await self._spool(original.stream, source_path)
                except AcquisitionError as error:
                    _raise_source_error(error)
                try:
                    async with _CONVERSION_SLOTS.slot():
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
        try:
            file: BinaryIO = await _blocking(
                lambda: path.open("wb"), cancel_cleanup=lambda opened: opened.close()
            )
        except OSError as error:
            raise ConversionSourceError("Conversion source failed integrity checks") from error

        body_error: BaseException | None = None
        try:
            async for chunk in stream:
                for offset in range(0, len(chunk), CACHE_CHUNK_SIZE):
                    part = chunk[offset : offset + CACHE_CHUNK_SIZE]
                    written = await _blocking(partial(file.write, part))
                    if written != len(part):
                        raise OSError("short temporary source write")
            await _blocking(file.flush)
        except OSError as error:
            body_error = error
            raise ConversionSourceError("Conversion source failed integrity checks") from error
        except BaseException as error:
            body_error = error
            raise
        finally:
            try:
                await _blocking(file.close)
            except OSError as error:
                if body_error is None:
                    raise ConversionSourceError(
                        "Conversion source failed integrity checks"
                    ) from error

    async def shutdown(self) -> None:
        await self._cache.shutdown()
