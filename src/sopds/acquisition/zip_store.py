"""Lifecycle-owned streaming of original ZIP members."""

import asyncio
import lzma
import os
import stat
import unicodedata
import zipfile
import zlib
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import IO, Any, BinaryIO, TypeVar

from sopds.acquisition.contracts import (
    AcquisitionAmbiguousMemberError,
    AcquisitionCorruptError,
    AcquisitionDirectoryMemberError,
    AcquisitionEncryptedMemberError,
    AcquisitionMemberNotFoundError,
    AcquisitionSizeMismatchError,
    AcquisitionStoreShutdownError,
    AcquisitionSymlinkMemberError,
    AcquisitionTarget,
    AcquisitionUnavailableError,
    AcquisitionUnsafePathError,
    ObservedOriginalStream,
    SourceRevision,
)

ZIP_WORKERS = 4
MAX_OPEN_STREAMS = 4
CHUNK_SIZE = 64 * 1024
_T = TypeVar("_T")

try:
    from compression import zstd as _zstd
except ImportError:  # pragma: no cover - Python versions before 3.14
    _DECODER_ERRORS: tuple[type[BaseException], ...] = (zlib.error, lzma.LZMAError)
else:
    _DECODER_ERRORS = (zlib.error, lzma.LZMAError, _zstd.ZstdError)

_CORRUPT_IO_ERRORS: tuple[type[BaseException], ...] = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    EOFError,
    RuntimeError,
    ValueError,
    OSError,
    *_DECODER_ERRORS,
)


class _OpenedMember:
    def __init__(
        self,
        archive: zipfile.ZipFile,
        member: IO[bytes],
        source: BinaryIO,
        revision: SourceRevision,
    ) -> None:
        self.archive = archive
        self.member = member
        self.source = source
        self.revision = revision

    def read(self) -> bytes:
        return self.member.read(CHUNK_SIZE)

    def close(self) -> None:
        try:
            self.member.close()
        finally:
            try:
                self.archive.close()
            finally:
                self.source.close()


def _validate_catalog_path(value: str) -> None:
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or any(unicodedata.category(character) == "Cc" for character in value)
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise AcquisitionUnsafePathError("Unsafe catalog path")


def _zip_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def _is_zip_directory(info: zipfile.ZipInfo) -> bool:
    dos_directory = info.create_system == 0 and bool(info.external_attr & 0x10)
    return info.is_dir() or stat.S_ISDIR(_zip_mode(info)) or dos_directory


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(_zip_mode(info))


def _open_binary(path: Path) -> BinaryIO:
    """Open only regular files without letting special objects block a worker."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError, NotADirectoryError, PermissionError:
        raise
    except OSError as error:
        try:
            mode = path.stat().st_mode
        except FileNotFoundError, NotADirectoryError, PermissionError, OSError:
            raise error from None
        if not stat.S_ISREG(mode):
            raise AcquisitionUnsafePathError("Original archive is not a regular file") from error
        raise

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AcquisitionUnsafePathError("Original archive is not a regular file")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _verify_opened_path(source: BinaryIO, expected_path: Path, resolved_root: Path) -> None:
    """Verify the opened object, rather than only the pathname checked before open."""
    proc_fd = Path("/proc/self/fd")
    try:
        if proc_fd.is_dir():
            actual_path = (proc_fd / str(source.fileno())).resolve(strict=True)
            if not _beneath(actual_path, resolved_root):
                raise AcquisitionUnsafePathError("Unsafe catalog path")
            return

        # Non-Linux fallback: require both a fresh in-root resolution and identity equality.
        actual_path = expected_path.resolve(strict=True)
        if not _beneath(actual_path, resolved_root):
            raise AcquisitionUnsafePathError("Unsafe catalog path")
        descriptor = os.fstat(source.fileno())
        named = actual_path.stat()
        if (descriptor.st_dev, descriptor.st_ino) != (named.st_dev, named.st_ino):
            raise AcquisitionUnsafePathError("Unsafe catalog path")
    except AcquisitionUnsafePathError:
        raise
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as error:
        raise AcquisitionUnavailableError("Original archive is unavailable") from error


def _close_archive_source(archive: zipfile.ZipFile | None, source: BinaryIO | None) -> None:
    try:
        if archive is not None:
            archive.close()
    finally:
        if source is not None:
            source.close()


def _inspect_member(
    root: Path, target: AcquisitionTarget
) -> tuple[zipfile.ZipFile, BinaryIO, zipfile.ZipInfo, SourceRevision]:
    """Open and validate archive metadata without reading the member body."""
    _validate_catalog_path(target.archive_relative_path)
    _validate_catalog_path(target.member_filename)
    try:
        resolved_root = root.resolve(strict=True)
        archive_path = (resolved_root / target.archive_relative_path).resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as error:
        raise AcquisitionUnavailableError("Original archive is unavailable") from error
    if not _beneath(archive_path, resolved_root):
        raise AcquisitionUnsafePathError("Unsafe catalog path")

    try:
        source = _open_binary(archive_path)
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as error:
        raise AcquisitionUnavailableError("Original archive is unavailable") from error

    archive: zipfile.ZipFile | None = None
    try:
        _verify_opened_path(source, archive_path, resolved_root)
        archive = zipfile.ZipFile(source, mode="r")
        matches = [info for info in archive.infolist() if info.filename == target.member_filename]
        if not matches:
            raise AcquisitionMemberNotFoundError("Original member is unavailable")
        if len(matches) != 1:
            raise AcquisitionAmbiguousMemberError("Original member is ambiguous")
        info = matches[0]
        if _is_zip_directory(info):
            raise AcquisitionDirectoryMemberError("Original member is a directory")
        if _is_zip_symlink(info):
            raise AcquisitionSymlinkMemberError("Original member is a symbolic link")
        if info.flag_bits & 0x1:
            raise AcquisitionEncryptedMemberError("Original member is encrypted")
        if info.file_size != target.expected_size:
            raise AcquisitionSizeMismatchError("Original size does not match catalog metadata")
        archive_stat = os.fstat(source.fileno())
        revision = SourceRevision(
            archive_size=archive_stat.st_size,
            archive_mtime_ns=archive_stat.st_mtime_ns,
            member_crc32=info.CRC,
        )
        return archive, source, info, revision
    except (
        AcquisitionMemberNotFoundError,
        AcquisitionAmbiguousMemberError,
        AcquisitionDirectoryMemberError,
        AcquisitionSymlinkMemberError,
        AcquisitionEncryptedMemberError,
        AcquisitionSizeMismatchError,
        AcquisitionUnsafePathError,
        AcquisitionUnavailableError,
        AcquisitionCorruptError,
    ):
        with suppress(*_CORRUPT_IO_ERRORS):
            _close_archive_source(archive, source)
        raise
    except asyncio.CancelledError:
        with suppress(*_CORRUPT_IO_ERRORS):
            _close_archive_source(archive, source)
        raise
    except _CORRUPT_IO_ERRORS as error:
        with suppress(*_CORRUPT_IO_ERRORS):
            _close_archive_source(archive, source)
        raise AcquisitionCorruptError("Original archive cannot be read") from error


def _describe_member(root: Path, target: AcquisitionTarget) -> SourceRevision:
    archive, source, _info, revision = _inspect_member(root, target)
    try:
        return revision
    finally:
        with suppress(*_CORRUPT_IO_ERRORS):
            _close_archive_source(archive, source)


def _open_member(root: Path, target: AcquisitionTarget) -> _OpenedMember:
    archive, source, info, revision = _inspect_member(root, target)
    try:
        member = archive.open(info, mode="r")
        return _OpenedMember(archive, member, source, revision)
    except asyncio.CancelledError:
        with suppress(*_CORRUPT_IO_ERRORS):
            _close_archive_source(archive, source)
        raise
    except _CORRUPT_IO_ERRORS as error:
        with suppress(*_CORRUPT_IO_ERRORS):
            _close_archive_source(archive, source)
        raise AcquisitionCorruptError("Original archive cannot be read") from error


class ZipOriginalStore:
    """Own workers and admission slots so ZIP I/O never blocks the event loop."""

    def __init__(self, library_root: Path) -> None:
        self._root = library_root
        self._executor = ThreadPoolExecutor(max_workers=ZIP_WORKERS, thread_name_prefix="sopds-zip")
        self._admission = asyncio.Semaphore(MAX_OPEN_STREAMS)
        self._state_lock = asyncio.Lock()
        self._streams: set[_ZipMemberStream] = set()
        self._opening: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._shutdown_task: asyncio.Task[None] | None = None

    async def _worker(self, function: Callable[[], _T]) -> _T:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, function)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            # The worker must finish before its ZIP objects can be closed by another worker.
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                await future
            raise

    async def _open_target(self, target: AcquisitionTarget) -> _OpenedMember:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, _open_member, self._root, target)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            opened: _OpenedMember | None = None
            with suppress(Exception):
                try:
                    opened = await asyncio.shield(future)
                except asyncio.CancelledError:
                    opened = await future
            if opened is not None:
                await self._worker(opened.close)
            raise

    async def _begin_open(self, task: asyncio.Task[Any]) -> None:
        async with self._state_lock:
            if self._closing:
                raise AcquisitionStoreShutdownError("Original store is shutting down")
            self._opening.add(task)

    async def _register_stream(self, stream: _ZipMemberStream) -> bool:
        async with self._state_lock:
            if self._closing:
                return False
            self._streams.add(stream)
            return True

    async def _cleanup_failed_open(
        self, stream: _ZipMemberStream | None, opened: _OpenedMember | None
    ) -> None:
        registered = False
        if stream is not None:
            async with self._state_lock:
                registered = stream in self._streams
        if registered and stream is not None:
            await stream.aclose()
        else:
            try:
                if opened is not None:
                    await self._worker(opened.close)
            finally:
                self._admission.release()

    async def describe(self, target: AcquisitionTarget) -> SourceRevision:
        async with self._state_lock:
            if self._closing:
                raise AcquisitionStoreShutdownError("Original store is shutting down")
        await self._admission.acquire()
        task = asyncio.current_task()
        if task is None:
            self._admission.release()
            raise RuntimeError("Acquisition must run inside an asyncio task")
        try:
            await self._begin_open(task)
            return await self._worker(lambda: _describe_member(self._root, target))
        finally:
            async with self._state_lock:
                self._opening.discard(task)
            self._admission.release()

    async def open(self, target: AcquisitionTarget) -> ObservedOriginalStream:
        async with self._state_lock:
            if self._closing:
                raise AcquisitionStoreShutdownError("Original store is shutting down")
        await self._admission.acquire()
        task = asyncio.current_task()
        if task is None:
            self._admission.release()
            raise RuntimeError("Acquisition must run inside an asyncio task")
        try:
            await self._begin_open(task)
        except BaseException:
            self._admission.release()
            raise

        opened: _OpenedMember | None = None
        stream: _ZipMemberStream | None = None
        try:
            opened = await self._open_target(target)
            stream = _ZipMemberStream(self, opened, target.expected_size)
            if not await self._register_stream(stream):
                raise AcquisitionStoreShutdownError("Original store is shutting down")
            return stream
        except BaseException:
            cleanup = asyncio.create_task(self._cleanup_failed_open(stream, opened))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            with suppress(Exception):
                cleanup.result()
            raise
        finally:
            async with self._state_lock:
                self._opening.discard(task)

    async def _released(self, stream: _ZipMemberStream) -> None:
        async with self._state_lock:
            if stream in self._streams:
                self._streams.remove(stream)
                self._admission.release()

    async def _run_shutdown(self) -> None:
        async with self._state_lock:
            opening = tuple(self._opening)
        if opening:
            await asyncio.gather(*opening, return_exceptions=True)
        async with self._state_lock:
            streams = tuple(self._streams)
        if streams:
            await asyncio.gather(*(stream.aclose() for stream in streams), return_exceptions=True)
        await asyncio.to_thread(self._executor.shutdown, wait=True, cancel_futures=True)

    async def shutdown(self) -> None:
        async with self._state_lock:
            if self._shutdown_task is None:
                self._closing = True
                self._shutdown_task = asyncio.create_task(self._run_shutdown())
            shutdown_task = self._shutdown_task
        await asyncio.shield(shutdown_task)


class _ZipMemberStream:
    def __init__(self, store: ZipOriginalStore, opened: _OpenedMember, expected_size: int) -> None:
        self._store = store
        self._opened = opened
        self._expected_size = expected_size
        self._count = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._iterated = False

    @property
    def source_revision(self) -> SourceRevision:
        return self._opened.revision

    def __aiter__(self) -> AsyncIterator[bytes]:
        if self._iterated:
            raise RuntimeError("Original streams are single-use")
        self._iterated = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        try:
            while True:
                async with self._lock:
                    if self._closed:
                        return
                    try:
                        chunk = await self._store._worker(self._opened.read)
                    except asyncio.CancelledError:
                        raise
                    except _CORRUPT_IO_ERRORS as error:
                        raise AcquisitionCorruptError("Original member cannot be read") from error
                if not chunk:
                    if self._count != self._expected_size:
                        raise AcquisitionSizeMismatchError(
                            "Streamed original size does not match catalog metadata"
                        )
                    return
                self._count += len(chunk)
                if self._count > self._expected_size:
                    raise AcquisitionSizeMismatchError("Streamed original exceeds catalog metadata")
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                try:
                    await self._store._worker(self._opened.close)
                except asyncio.CancelledError:
                    raise
                except _CORRUPT_IO_ERRORS as error:
                    raise AcquisitionCorruptError("Original member cannot be read") from error
            finally:
                await self._store._released(self)
