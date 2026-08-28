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
from typing import IO, Any, BinaryIO, NoReturn, TypeVar, cast

from sopds.acquisition.contracts import (
    AcquisitionAmbiguousMemberError,
    AcquisitionCorruptError,
    AcquisitionDirectoryMemberError,
    AcquisitionEncryptedMemberError,
    AcquisitionError,
    AcquisitionMemberNotFoundError,
    AcquisitionSizeMismatchError,
    AcquisitionSourceIOError,
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
    *_DECODER_ERRORS,
)


class _SourceFileIOError(Exception):
    """Mark failures raised by the archive file rather than a member decoder."""

    def __init__(self, error: OSError) -> None:
        super().__init__(str(error))
        self.error = error


class _SourceFile:
    """Keep filesystem failures distinguishable after the file enters zipfile."""

    def __init__(self, source: BinaryIO) -> None:
        self._source = source

    @property
    def closed(self) -> bool:
        return self._source.closed

    def read(self, size: int = -1) -> bytes:
        try:
            return self._source.read(size)
        except OSError as error:
            raise _SourceFileIOError(error) from error

    def _seek_before_start(self, offset: int, whence: int) -> bool:
        try:
            if whence == os.SEEK_SET:
                return offset < 0
            if whence == os.SEEK_CUR:
                return self._source.tell() + offset < 0
            if whence == os.SEEK_END:
                return os.fstat(self._source.fileno()).st_size + offset < 0
            return False
        except OSError as error:
            raise _SourceFileIOError(error) from error

    def seek(self, offset: int, whence: int = 0) -> int:
        if self._seek_before_start(offset, whence):
            return self._source.seek(offset, whence)
        try:
            return self._source.seek(offset, whence)
        except OSError as error:
            raise _SourceFileIOError(error) from error

    def tell(self) -> int:
        try:
            return self._source.tell()
        except OSError as error:
            raise _SourceFileIOError(error) from error

    def fileno(self) -> int:
        try:
            return self._source.fileno()
        except OSError as error:
            raise _SourceFileIOError(error) from error

    def readable(self) -> bool:
        try:
            return self._source.readable()
        except OSError as error:
            raise _SourceFileIOError(error) from error

    def seekable(self) -> bool:
        try:
            return self._source.seekable()
        except OSError as error:
            raise _SourceFileIOError(error) from error

    def close(self) -> None:
        try:
            self._source.close()
        except OSError as error:
            raise _SourceFileIOError(error) from error


def _failure_priority(error: BaseException) -> int:
    """Keep control-flow failures, then fatal failures, ahead of disappearance."""
    if not isinstance(error, Exception):
        return 2
    if isinstance(error, _SourceFileIOError):
        error = error.error
    if isinstance(
        error,
        (
            AcquisitionUnavailableError,
            AcquisitionMemberNotFoundError,
            FileNotFoundError,
            NotADirectoryError,
        ),
    ):
        return 0
    return 1


def _preferred_failure(primary: BaseException, cleanup: BaseException) -> BaseException:
    if _failure_priority(cleanup) > _failure_priority(primary):
        return cleanup
    return primary


def _close_resources(*resources: Any) -> None:
    failure: BaseException | None = None
    for resource in resources:
        if resource is None:
            continue
        try:
            resource.close()
        except BaseException as error:
            failure = error if failure is None else _preferred_failure(failure, error)
    if failure is not None:
        raise failure


def _raise_acquisition_failure(
    error: BaseException,
    *,
    unavailable_message: str,
    source_io_message: str,
    corrupt_message: str,
    unmarked_os_error_is_corrupt: bool = False,
) -> NoReturn:
    if isinstance(error, (asyncio.CancelledError, AcquisitionError)):
        raise error
    if isinstance(error, _SourceFileIOError):
        if isinstance(error.error, (FileNotFoundError, NotADirectoryError)):
            raise AcquisitionUnavailableError(unavailable_message) from error.error
        raise AcquisitionSourceIOError(source_io_message) from error.error
    if isinstance(error, (FileNotFoundError, NotADirectoryError)):
        raise AcquisitionUnavailableError(unavailable_message) from error
    if isinstance(error, OSError):
        if unmarked_os_error_is_corrupt:
            raise AcquisitionCorruptError(corrupt_message) from error
        raise AcquisitionSourceIOError(source_io_message) from error
    if isinstance(error, _CORRUPT_IO_ERRORS):
        raise AcquisitionCorruptError(corrupt_message) from error
    raise error


def _close_archive_after_failure(
    primary: BaseException,
    archive: zipfile.ZipFile | None,
    source: BinaryIO | None,
) -> NoReturn:
    try:
        _close_archive_source(archive, source)
    except BaseException as cleanup:
        primary = _preferred_failure(primary, cleanup)
    _raise_acquisition_failure(
        primary,
        unavailable_message="Original archive is unavailable",
        source_io_message="Original archive cannot be accessed",
        corrupt_message="Original archive cannot be read",
        unmarked_os_error_is_corrupt=True,
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
        _close_resources(self.member, self.archive, self.source)


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
    except (FileNotFoundError, NotADirectoryError) as error:
        raise AcquisitionUnavailableError("Original archive is unavailable") from error
    except OSError as error:
        raise AcquisitionSourceIOError("Original archive cannot be accessed") from error


def _close_archive_source(archive: zipfile.ZipFile | None, source: BinaryIO | None) -> None:
    _close_resources(archive, source)


def _inspect_member(
    root: Path, target: AcquisitionTarget
) -> tuple[zipfile.ZipFile, BinaryIO, zipfile.ZipInfo, SourceRevision]:
    """Open and validate archive metadata without reading the member body."""
    _validate_catalog_path(target.archive_relative_path)
    _validate_catalog_path(target.member_filename)
    try:
        resolved_root = root.resolve(strict=True)
        archive_path = (resolved_root / target.archive_relative_path).resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as error:
        raise AcquisitionUnavailableError("Original archive is unavailable") from error
    except OSError as error:
        raise AcquisitionSourceIOError("Original archive cannot be accessed") from error
    if not _beneath(archive_path, resolved_root):
        raise AcquisitionUnsafePathError("Unsafe catalog path")

    try:
        source = cast(BinaryIO, _SourceFile(_open_binary(archive_path)))
    except (FileNotFoundError, NotADirectoryError) as error:
        raise AcquisitionUnavailableError("Original archive is unavailable") from error
    except OSError as error:
        raise AcquisitionSourceIOError("Original archive cannot be accessed") from error

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
        try:
            archive_stat = os.fstat(source.fileno())
        except OSError as error:
            raise _SourceFileIOError(error) from error
        revision = SourceRevision(
            archive_size=archive_stat.st_size,
            archive_mtime_ns=archive_stat.st_mtime_ns,
            member_crc32=info.CRC,
        )
        return archive, source, info, revision
    except BaseException as error:
        _close_archive_after_failure(error, archive, source)


def _describe_member(root: Path, target: AcquisitionTarget) -> SourceRevision:
    archive, source, _info, revision = _inspect_member(root, target)
    try:
        _close_archive_source(archive, source)
    except BaseException as error:
        _raise_acquisition_failure(
            error,
            unavailable_message="Original archive is unavailable",
            source_io_message="Original archive cannot be accessed",
            corrupt_message="Original archive cannot be read",
            unmarked_os_error_is_corrupt=True,
        )
    return revision


def _open_member(root: Path, target: AcquisitionTarget) -> _OpenedMember:
    archive, source, info, revision = _inspect_member(root, target)
    try:
        member = archive.open(info, mode="r")
        return _OpenedMember(archive, member, source, revision)
    except BaseException as error:
        _close_archive_after_failure(error, archive, source)


class ZipOriginalStore:
    """Own workers and admission slots so ZIP I/O never blocks the event loop."""

    def __init__(self, library_root: Path) -> None:
        self._root = library_root
        self._executor = ThreadPoolExecutor(max_workers=ZIP_WORKERS, thread_name_prefix="sopds-zip")
        self._admission = asyncio.Semaphore(MAX_OPEN_STREAMS)
        self._state_lock = asyncio.Lock()
        self._streams: set[_ZipMemberStream] = set()
        self._opening: set[asyncio.Future[None]] = set()
        self._closing = False
        self._shutdown_task: asyncio.Task[None] | None = None

    async def _finish_worker(self, future: asyncio.Future[_T]) -> _T:
        """Consume a worker result despite repeated cancellation of its caller."""
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                continue
            except BaseException:
                break
        return future.result()

    async def _worker(self, function: Callable[[], _T]) -> _T:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, function)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError as cancellation:
            # ZIP objects cannot be closed until the worker using them has finished.
            with suppress(BaseException):
                await self._finish_worker(future)
            raise cancellation

    async def _open_target(self, target: AcquisitionTarget) -> _OpenedMember:
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(self._executor, _open_member, self._root, target)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError as cancellation:
            opened: _OpenedMember | None = None
            with suppress(BaseException):
                opened = await self._finish_worker(future)
            if opened is not None:
                with suppress(BaseException):
                    await self._worker(opened.close)
            raise cancellation

    async def _wait_for_task(self, task: asyncio.Task[_T]) -> _T:
        """Let owned bookkeeping finish before propagating caller cancellation."""
        caller = asyncio.current_task()
        initial_cancellations = caller.cancelling() if caller is not None else 0
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.wait((task,))
            except asyncio.CancelledError as error:
                cancellation = error
        was_cancelled = cancellation is not None or (
            caller is not None and caller.cancelling() > initial_cancellations
        )
        if was_cancelled:
            with suppress(BaseException):
                task.result()
            if cancellation is not None:
                raise cancellation
            raise asyncio.CancelledError
        return task.result()

    async def _begin_open(self) -> asyncio.Future[None]:
        async with self._state_lock:
            if self._closing:
                raise AcquisitionStoreShutdownError("Original store is shutting down")
            token = asyncio.get_running_loop().create_future()
            self._opening.add(token)
            return token

    async def _complete_opening(self, token: asyncio.Future[None]) -> None:
        async with self._state_lock:
            self._opening.discard(token)
            if not token.done():
                token.set_result(None)

    async def _register_stream(self, stream: _ZipMemberStream) -> bool:
        async with self._state_lock:
            if self._closing:
                return False
            self._streams.add(stream)
            return True

    async def _cleanup_failed_open(
        self,
        token: asyncio.Future[None] | None,
        stream: _ZipMemberStream | None,
        opened: _OpenedMember | None,
    ) -> None:
        failure: BaseException | None = None
        try:
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
        except BaseException as error:
            failure = error
        if token is not None:
            try:
                await self._complete_opening(token)
            except BaseException as error:
                failure = error if failure is None else _preferred_failure(failure, error)
        if failure is not None:
            raise failure

    async def _finish_description(self, token: asyncio.Future[None] | None) -> None:
        self._admission.release()
        if token is not None:
            await self._complete_opening(token)

    async def describe(self, target: AcquisitionTarget) -> SourceRevision:
        async with self._state_lock:
            if self._closing:
                raise AcquisitionStoreShutdownError("Original store is shutting down")
        await self._admission.acquire()
        token: asyncio.Future[None] | None = None
        try:
            token = await self._begin_open()
            return await self._worker(lambda: _describe_member(self._root, target))
        finally:
            cleanup = asyncio.create_task(self._finish_description(token))
            await self._wait_for_task(cleanup)

    async def open(self, target: AcquisitionTarget) -> ObservedOriginalStream:
        async with self._state_lock:
            if self._closing:
                raise AcquisitionStoreShutdownError("Original store is shutting down")
        await self._admission.acquire()
        token: asyncio.Future[None] | None = None
        opened: _OpenedMember | None = None
        stream: _ZipMemberStream | None = None
        try:
            token = await self._begin_open()
            opened = await self._open_target(target)
            stream = _ZipMemberStream(self, opened, target.expected_size)
            if not await self._register_stream(stream):
                raise AcquisitionStoreShutdownError("Original store is shutting down")
            handoff = asyncio.create_task(self._complete_opening(token))
            await self._wait_for_task(handoff)
            return stream
        except BaseException as error:
            cleanup = asyncio.create_task(self._cleanup_failed_open(token, stream, opened))
            try:
                await self._wait_for_task(cleanup)
            except BaseException as cleanup_error:
                error = _preferred_failure(error, cleanup_error)
            _raise_acquisition_failure(
                error,
                unavailable_message="Original archive is unavailable",
                source_io_message="Original archive cannot be accessed",
                corrupt_message="Original member cannot be read",
            )

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
        self._cleanup_task: asyncio.Task[None] | None = None
        self._physically_closed = False
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
        failure: BaseException | None = None
        try:
            while True:
                async with self._lock:
                    if self._closed:
                        break
                    try:
                        chunk = await self._store._worker(self._opened.read)
                    except BaseException as error:
                        _raise_acquisition_failure(
                            error,
                            unavailable_message="Original archive is unavailable",
                            source_io_message="Original archive cannot be accessed",
                            corrupt_message="Original member cannot be read",
                            unmarked_os_error_is_corrupt=True,
                        )
                if not chunk:
                    if self._count != self._expected_size:
                        raise AcquisitionSizeMismatchError(
                            "Streamed original size does not match catalog metadata"
                        )
                    break
                self._count += len(chunk)
                if self._count > self._expected_size:
                    raise AcquisitionSizeMismatchError("Streamed original exceeds catalog metadata")
                yield chunk
        except BaseException as error:
            failure = error
        try:
            await self.aclose()
        except BaseException as cleanup_error:
            if failure is None:
                raise
            failure = _preferred_failure(failure, cleanup_error)
        if failure is not None:
            raise failure.with_traceback(failure.__traceback__)

    async def _cleanup(self) -> None:
        failure: BaseException | None = None
        async with self._lock:
            if self._closed:
                return
            if not self._physically_closed:
                try:
                    try:
                        await self._store._worker(self._opened.close)
                    except BaseException as error:
                        _raise_acquisition_failure(
                            error,
                            unavailable_message="Original archive is unavailable",
                            source_io_message="Original archive cannot be accessed",
                            corrupt_message="Original member cannot be read",
                            unmarked_os_error_is_corrupt=True,
                        )
                except BaseException as error:
                    failure = error
                finally:
                    self._physically_closed = True
            try:
                await self._store._released(self)
            except BaseException as error:
                failure = error if failure is None else _preferred_failure(failure, error)
            else:
                self._closed = True
        if failure is not None:
            raise failure

    async def aclose(self) -> None:
        if self._closed:
            return
        task = self._cleanup_task
        if task is None or task.done():
            task = asyncio.create_task(self._cleanup())
            self._cleanup_task = task
        await self._store._wait_for_task(task)
