"""Filesystem-backed conversion artifact cache with process-local leases."""

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import BinaryIO, TypeVar

from sopds.conversion.contracts import (
    ArtifactProducer,
    ArtifactResult,
    CacheCleanupSummary,
    ConversionShutdownError,
    ConversionSourceKey,
    InvalidConversionOutputError,
    normalize_format,
)

CACHE_CHUNK_SIZE = 64 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")
_T = TypeVar("_T")
_LOGGER = logging.getLogger(__name__)


class _InvalidCachedArtifact(Exception):
    """An artifact pathname no longer refers to a safe cached file."""


@dataclass(slots=True)
class _Operation:
    """Track callers sharing one producer so abandonment can stop real work."""

    task: asyncio.Task[Path]
    retired: asyncio.Event
    waiters: int = 0
    accepting: bool = True


def cache_digest(key: ConversionSourceKey) -> str:
    """Hash a complete canonical source/converter identity without filesystem text."""
    revision = key.revision
    payload = {
        "converter": {"name": key.converter.name, "version": key.converter.version},
        "public_id": key.public_id,
        "revision": {
            "archive_mtime_ns": revision.archive_mtime_ns,
            "archive_size": revision.archive_size,
            "member_crc32": revision.member_crc32,
        },
        "source_format": key.source_format,
        "target_format": key.target_format,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _safe_remove(path: Path) -> bool | None:
    """Remove one cache entry without following it or traversing a directory."""
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    except IsADirectoryError:
        try:
            path.rmdir()
        except FileNotFoundError:
            return None
        except OSError:
            return False
    except OSError:
        return False
    return True


def _discard(path: Path) -> None:
    _safe_remove(path)


def _artifact_stat(path: Path) -> os.stat_result | None:
    try:
        result = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if not stat.S_ISREG(result.st_mode) or result.st_size <= 0:
        _safe_remove(path)
        return None
    return result


def _open_artifact(path: Path) -> tuple[BinaryIO, int]:
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"):
        flags |= getattr(os, name, 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _InvalidCachedArtifact from error
    try:
        result = os.fstat(descriptor)
        if not stat.S_ISREG(result.st_mode) or result.st_size <= 0:
            raise _InvalidCachedArtifact
        return os.fdopen(descriptor, "rb"), result.st_size
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


class ArtifactCache:
    """Own cache workers, shared producers, artifact descriptors, and temp files."""

    def __init__(self, cache_dir: Path, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            raise ValueError("Cache TTL must be positive")
        self._dir = cache_dir
        self._ttl = ttl_seconds
        self._executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="sopds-conversion")
        self._lock = asyncio.Lock()
        self._operations: dict[str, _Operation] = {}
        self._bookkeeping: set[asyncio.Task[None]] = set()
        self._admitted: set[asyncio.Future[None]] = set()
        self._leases: set[_ArtifactStream] = set()
        self._active: dict[str, int] = {}
        self._inactive: dict[str, asyncio.Event] = {}
        self._working_paths: set[Path] = set()
        self._closing = False
        self._shutdown_task: asyncio.Task[None] | None = None
        self._started = False

    async def _worker(
        self,
        function: Callable[[], _T],
        *,
        cancel_cleanup: Callable[[_T], None] | None = None,
    ) -> _T:
        future = asyncio.get_running_loop().run_in_executor(self._executor, function)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            exception = future.exception()
            if exception is None and cancel_cleanup is not None:
                cancel_cleanup(future.result())
            raise

    async def startup(self) -> None:
        async with self._lock:
            if self._closing:
                raise ConversionShutdownError("Conversion cache is shutting down")
            if self._started:
                return
            self._started = True
            await self._worker(lambda: self._dir.mkdir(parents=True, exist_ok=True))
        summary = await self.cleanup()
        if summary.removed_files:
            _LOGGER.info(
                f"Startup conversion cache cleanup removed files phase=startup_cleanup "
                f"removed_files={summary.removed_files}"
            )
        if summary.failed_entries:
            _LOGGER.warning(
                f"Startup conversion cache cleanup failed entries "
                f"phase=startup_cleanup failed_entries={summary.failed_entries}"
            )

    def _artifact_path(self, digest: str) -> Path:
        if not _DIGEST.fullmatch(digest):
            raise ValueError("Invalid cache digest")
        return self._dir / f"{digest}.artifact"

    async def _wait_until_inactive(self, digest: str) -> None:
        while True:
            async with self._lock:
                if self._active.get(digest, 0) == 0:
                    return
                event = self._inactive.setdefault(digest, asyncio.Event())
            await event.wait()

    async def _resolve(self, digest: str, producer: ArtifactProducer) -> Path:
        artifact = self._artifact_path(digest)
        result = await self._worker(lambda: _artifact_stat(artifact))
        if result is not None:
            age_ns = time.time_ns() - result.st_mtime_ns
            if 0 <= age_ns < self._ttl * 1_000_000_000:
                return artifact
            await self._wait_until_inactive(digest)
            await self._worker(lambda: _safe_remove(artifact))

        output = self._dir / f"{digest}.{os.urandom(16).hex()}.tmp"
        async with self._lock:
            self._working_paths.add(output)
        try:
            await producer(output)
            try:
                output_stat = await self._worker(lambda: output.stat(follow_symlinks=False))
            except FileNotFoundError:
                raise InvalidConversionOutputError("Converter produced no valid output") from None
            if not stat.S_ISREG(output_stat.st_mode) or output_stat.st_size <= 0:
                raise InvalidConversionOutputError("Converter produced no valid output")
            await self._worker(lambda: os.utime(output, None, follow_symlinks=False))
            await self._worker(lambda: output.replace(artifact))
            return artifact
        except InvalidConversionOutputError:
            raise
        except FileNotFoundError:
            raise InvalidConversionOutputError("Converter produced no valid output") from None
        except OSError as error:
            raise InvalidConversionOutputError(
                "Conversion output could not be published"
            ) from error
        finally:
            async with self._lock:
                self._working_paths.discard(output)
            await self._worker(lambda: _safe_remove(output))

    def _remember_operation(self, digest: str, operation: _Operation) -> None:
        def forget(_completed: asyncio.Task[Path]) -> None:
            bookkeeping = asyncio.create_task(self._forget_operation(digest, operation))
            self._bookkeeping.add(bookkeeping)
            bookkeeping.add_done_callback(self._bookkeeping.discard)

        self._operations[digest] = operation
        operation.task.add_done_callback(forget)

    async def _operation_for(self, digest: str, producer: ArtifactProducer) -> _Operation:
        while True:
            async with self._lock:
                operation = self._operations.get(digest)
                if operation is None:
                    if self._closing:
                        raise ConversionShutdownError("Conversion cache is shutting down")
                    task = asyncio.create_task(
                        self._resolve(digest, producer), name=f"conversion-{digest}"
                    )
                    operation = _Operation(task, asyncio.Event())
                    self._remember_operation(digest, operation)
                if operation.accepting:
                    operation.waiters += 1
                    return operation
                retired = operation.retired
            await retired.wait()

    async def _release_waiter(self, operation: _Operation) -> None:
        drain: asyncio.Task[Path] | None = None
        async with self._lock:
            if operation.waiters <= 0:
                return
            operation.waiters -= 1
            if operation.waiters == 0 and operation.accepting and not operation.task.done():
                operation.accepting = False
                operation.task.cancel()
                drain = operation.task
        if drain is not None:
            await self._drain_operation(drain)

    @staticmethod
    async def _drain_operation(operation: asyncio.Task[Path]) -> None:
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        with suppress(BaseException):
            operation.result()

    async def _await_operation(self, operation: _Operation) -> Path:
        try:
            return await asyncio.shield(operation.task)
        finally:
            release = asyncio.create_task(self._release_waiter(operation))
            cancelled = False
            while not release.done():
                try:
                    await asyncio.shield(release)
                except asyncio.CancelledError:
                    cancelled = True
            release.result()
            if cancelled:
                raise asyncio.CancelledError

    async def get_or_create(
        self, key: ConversionSourceKey, producer: ArtifactProducer
    ) -> ArtifactResult:
        loop = asyncio.get_running_loop()
        transition = loop.create_future()
        async with self._lock:
            if self._closing:
                raise ConversionShutdownError("Conversion cache is shutting down")
            if not self._started:
                raise RuntimeError("Conversion cache has not started")
            self._admitted.add(transition)
        try:
            return await self._get_or_create(cache_digest(key), producer)
        finally:
            async with self._lock:
                self._admitted.discard(transition)
                if not transition.done():
                    transition.set_result(None)

    async def _get_or_create(self, digest: str, producer: ArtifactProducer) -> ArtifactResult:
        for attempt in range(2):
            operation = await self._operation_for(digest, producer)
            path = await self._await_operation(operation)

            async with self._lock:
                self._active[digest] = self._active.get(digest, 0) + 1
            transferred = False
            try:
                try:
                    file, length = await self._worker(
                        partial(_open_artifact, path),
                        cancel_cleanup=lambda opened: opened[0].close(),
                    )
                except _InvalidCachedArtifact:
                    await self._worker(partial(_safe_remove, path))
                    async with self._lock:
                        if self._operations.get(digest) is operation:
                            self._operations.pop(digest, None)
                        operation.retired.set()
                    if attempt == 0:
                        continue
                    raise InvalidConversionOutputError(
                        "Cached conversion output is invalid"
                    ) from None

                stream = _ArtifactStream(self, digest, file)
                try:
                    async with self._lock:
                        self._leases.add(stream)
                        if self._operations.get(digest) is operation:
                            self._operations.pop(digest, None)
                        operation.retired.set()
                    transferred = True
                    return ArtifactResult(content_length=length, stream=stream)
                except BaseException:
                    await stream.aclose()
                    transferred = True
                    raise
            finally:
                if not transferred:
                    await self._release_digest(digest)
        raise AssertionError("unreachable")

    async def _forget_operation(self, digest: str, operation: _Operation) -> None:
        async with self._lock:
            if self._operations.get(digest) is operation:
                self._operations.pop(digest, None)
            operation.retired.set()

    async def create_source_path(self, digest: str, source_format: str) -> Path:
        canonical_format = normalize_format(source_format)

        def create() -> Path:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f"{digest}.", suffix=f".source.{canonical_format}", dir=self._dir
            )
            path = Path(raw_path)
            try:
                os.close(descriptor)
            except BaseException:
                _safe_remove(path)
                raise
            return path

        async with self._lock:
            if self._closing:
                raise ConversionShutdownError("Conversion cache is shutting down")
            path = await self._worker(create, cancel_cleanup=_discard)
            self._working_paths.add(path)
            return path

    async def remove_source_path(self, path: Path) -> None:
        async with self._lock:
            self._working_paths.discard(path)
        await self._worker(lambda: _safe_remove(path))

    async def _release_digest(self, digest: str) -> None:
        async with self._lock:
            count = self._active.get(digest, 0)
            if count <= 1:
                self._active.pop(digest, None)
                event = self._inactive.pop(digest, None)
                if event is not None:
                    event.set()
            else:
                self._active[digest] = count - 1

    async def _released(self, stream: _ArtifactStream, digest: str) -> None:
        async with self._lock:
            self._leases.discard(stream)
        await self._release_digest(digest)

    async def cleanup(self) -> CacheCleanupSummary:
        async with self._lock:
            if self._closing:
                return CacheCleanupSummary(0, 0)

            def clean() -> CacheCleanupSummary:
                removed_files = failed_entries = 0

                def account(result: bool | None) -> None:
                    nonlocal removed_files, failed_entries
                    if result is True:
                        removed_files += 1
                    elif result is False:
                        failed_entries += 1

                self._dir.mkdir(parents=True, exist_ok=True)
                now_ns = time.time_ns()
                for path in self._dir.iterdir():
                    if path in self._working_paths:
                        continue
                    name = path.name
                    if name.endswith(".tmp") or name.endswith(".source") or ".source." in name:
                        account(_safe_remove(path))
                        continue
                    if not name.endswith(".artifact"):
                        continue
                    digest = name.removesuffix(".artifact")
                    if digest in self._active or not _DIGEST.fullmatch(digest):
                        continue
                    try:
                        result = path.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        failed_entries += 1
                        continue
                    if not stat.S_ISREG(result.st_mode) or result.st_size <= 0:
                        account(_safe_remove(path))
                        continue
                    age_ns = now_ns - result.st_mtime_ns
                    if age_ns < 0 or age_ns >= self._ttl * 1_000_000_000:
                        account(_safe_remove(path))
                return CacheCleanupSummary(removed_files, failed_entries)

            # Registration/admission uses the same lock, making the scan atomic with
            # every working path and active artifact becoming visible.
            return await self._worker(clean)

    async def _run_shutdown(self) -> None:
        async with self._lock:
            operations = tuple(self._operations.values())
            to_cancel: list[asyncio.Task[Path]] = []
            for operation in operations:
                if operation.accepting:
                    operation.accepting = False
                    to_cancel.append(operation.task)
            admitted = tuple(self._admitted)
        for task in to_cancel:
            task.cancel()
        if operations:
            await asyncio.gather(
                *(operation.task for operation in operations), return_exceptions=True
            )
        if admitted:
            await asyncio.gather(*admitted, return_exceptions=True)

        async with self._lock:
            leases = tuple(self._leases)
        if leases:
            await asyncio.gather(*(lease.aclose() for lease in leases), return_exceptions=True)

        while True:
            await asyncio.sleep(0)
            async with self._lock:
                bookkeeping = tuple(self._bookkeeping)
            if not bookkeeping:
                break
            await asyncio.gather(*bookkeeping, return_exceptions=True)
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)

    async def shutdown(self) -> None:
        async with self._lock:
            if self._shutdown_task is None:
                self._closing = True
                self._shutdown_task = asyncio.create_task(
                    self._run_shutdown(), name="conversion-cache-shutdown"
                )
            task = self._shutdown_task
        await asyncio.shield(task)


class _ArtifactStream:
    def __init__(self, cache: ArtifactCache, digest: str, file: BinaryIO) -> None:
        self._cache = cache
        self._digest = digest
        self._file = file
        self._lock = asyncio.Lock()
        self._closed = False
        self._iterated = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        if self._iterated:
            raise RuntimeError("Artifact streams are single-use")
        self._iterated = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        try:
            while True:
                async with self._lock:
                    if self._closed:
                        return
                    chunk = await self._cache._worker(lambda: self._file.read(CACHE_CHUNK_SIZE))
                if not chunk:
                    return
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self._cache._worker(self._file.close)
            finally:
                await self._cache._released(self, self._digest)
