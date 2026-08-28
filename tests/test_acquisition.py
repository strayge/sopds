"""Original acquisition, streaming, and presentation tests."""

import asyncio
import io
import os
import stat
import struct
import threading
import zipfile
import zlib
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest

from sopds.acquisition import zip_store
from sopds.acquisition.contracts import (
    AcquisitionAmbiguousMemberError,
    AcquisitionCorruptError,
    AcquisitionDirectoryMemberError,
    AcquisitionEncryptedMemberError,
    AcquisitionMemberNotFoundError,
    AcquisitionSizeMismatchError,
    AcquisitionSourceIOError,
    AcquisitionStoreShutdownError,
    AcquisitionSymlinkMemberError,
    AcquisitionTarget,
    AcquisitionUnavailableError,
    AcquisitionUnsafePathError,
    AsyncByteStream,
    ObservedOriginalStream,
    SourceRevision,
)
from sopds.acquisition.service import (
    AcquisitionService,
    content_disposition,
    media_type_for,
    safe_download_filename,
)
from sopds.acquisition.zip_store import (
    CHUNK_SIZE,
    MAX_OPEN_STREAMS,
    ZipOriginalStore,
    _OpenedMember,
)


def _target(
    *,
    archive: str = "books.zip",
    member: str = "original.fb2",
    size: int = 7,
) -> AcquisitionTarget:
    return AcquisitionTarget(
        generation_id=3,
        public_id="public",
        title="Книга",
        expected_size=size,
        original_format="fb2",
        archive_relative_path=archive,
        member_filename=member,
    )


class _TargetRepository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int | None]] = []

    async def acquisition_target(
        self,
        public_id: str,
        *,
        expected_generation_id: int | None = None,
    ) -> AcquisitionTarget | None:
        self.calls.append((public_id, expected_generation_id))
        return _target()


class _DescriptionStore:
    async def describe(self, _target: AcquisitionTarget) -> SourceRevision:
        return SourceRevision(1, 2, 3)

    async def open(self, _target: AcquisitionTarget) -> ObservedOriginalStream:
        raise AssertionError("This test only describes originals")

    async def shutdown(self) -> None:
        pass


def _zip(path: Path, members: list[tuple[str | zipfile.ZipInfo, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members:
            archive.writestr(name, body)


async def _body(stream: AsyncByteStream) -> bytes:
    chunks = [chunk async for chunk in stream]
    return b"".join(chunks)


def _assert_no_extraction(root: Path) -> None:
    assert not list(root.glob("**/original.fb2"))


async def test_acquisition_service_forwards_optional_expected_generation() -> None:
    repository = _TargetRepository()
    service = AcquisitionService(repository, _DescriptionStore())

    current = await service.describe("public")
    expected = await service.describe("public", expected_generation_id=3)

    assert current.public_id == expected.public_id == "public"
    assert repository.calls == [("public", None), ("public", 3)]


async def test_description_revision_uses_archive_and_member_crc32(tmp_path: Path) -> None:
    archive_path = tmp_path / "books.zip"
    _zip(archive_path, [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)

    first = await store.describe(_target())
    second = await store.describe(_target())
    stream = await store.open(_target())

    archive_stat = archive_path.stat()
    assert first == second == stream.source_revision
    assert first.archive_size == archive_stat.st_size
    assert first.archive_mtime_ns == archive_stat.st_mtime_ns
    assert first.member_crc32 == zlib.crc32(b"content")
    await stream.aclose()

    os.utime(
        archive_path,
        ns=(archive_stat.st_atime_ns, archive_stat.st_mtime_ns + 1_000_000_000),
    )
    changed = await store.describe(_target())

    assert changed != first
    assert changed.archive_mtime_ns != first.archive_mtime_ns
    await store.shutdown()


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (FileNotFoundError("disappeared during revision stat"), AcquisitionUnavailableError),
        (NotADirectoryError("path disappeared during revision stat"), AcquisitionUnavailableError),
        (PermissionError("revision stat denied"), AcquisitionSourceIOError),
        (OSError("revision stat failed"), AcquisitionSourceIOError),
    ],
)
async def test_revision_stat_failures_are_classified_as_source_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    expected_error: type[Exception],
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    original_directory_check = zip_store._is_zip_directory
    original_fstat = os.fstat
    revision_stat_pending = False

    def arm_revision_stat(info: zipfile.ZipInfo) -> bool:
        nonlocal revision_stat_pending
        result = original_directory_check(info)
        revision_stat_pending = True
        return result

    def fail_revision_stat(descriptor: int) -> os.stat_result:
        nonlocal revision_stat_pending
        if revision_stat_pending:
            revision_stat_pending = False
            raise error
        return original_fstat(descriptor)

    monkeypatch.setattr(zip_store, "_is_zip_directory", arm_revision_stat)
    monkeypatch.setattr(os, "fstat", fail_revision_stat)
    store = ZipOriginalStore(tmp_path)

    with pytest.raises(expected_error) as raised:
        await store.describe(_target())

    assert raised.value.__cause__ is error
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (FileNotFoundError("disappeared during close"), AcquisitionUnavailableError),
        (NotADirectoryError("disappeared during close"), AcquisitionUnavailableError),
        (PermissionError("denied during close"), AcquisitionSourceIOError),
        (OSError("I/O failed during close"), AcquisitionSourceIOError),
    ],
)
async def test_description_classifies_wrapped_source_close_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
    expected_error: type[Exception],
) -> None:
    class FailingCloseSource:
        def __init__(self, source: BinaryIO) -> None:
            self._source = source

        def close(self) -> None:
            self._source.close()
            raise error

        def __getattr__(self, name: str) -> Any:
            return getattr(self._source, name)

    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    original_open = zip_store._open_binary

    def open_with_failing_close(path: Path) -> BinaryIO:
        return cast(BinaryIO, FailingCloseSource(original_open(path)))

    monkeypatch.setattr(zip_store, "_open_binary", open_with_failing_close)
    store = ZipOriginalStore(tmp_path)
    with pytest.raises(expected_error) as raised:
        await store.describe(_target())

    assert raised.value.__cause__ is error
    await store.shutdown()


async def test_member_close_attempts_every_resource_and_keeps_fatal_failure() -> None:
    class CloseProbe:
        def __init__(self, error: BaseException | None = None) -> None:
            self.error = error
            self.closed = False

        def close(self) -> None:
            self.closed = True
            if self.error is not None:
                raise self.error

    disappearance = FileNotFoundError("member disappeared")
    fatal = OSError("archive close failed")
    member = CloseProbe(disappearance)
    archive = CloseProbe(fatal)
    source = CloseProbe()
    opened = _OpenedMember(
        cast(Any, archive),
        cast(Any, member),
        cast(Any, source),
        SourceRevision(1, 2, 3),
    )

    with pytest.raises(OSError) as raised:
        opened.close()

    assert raised.value is fatal
    assert member.closed and archive.closed and source.closed


async def test_inspection_unmarked_close_oserror_is_corrupt_and_outranks_missing_member(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("other.fb2", b"content")])
    original_close = zip_store._close_archive_source
    closed: list[tuple[zipfile.ZipFile | None, BinaryIO | None]] = []
    fatal = OSError("cleanup failed")

    def close_then_fail(archive: zipfile.ZipFile | None, source: BinaryIO | None) -> None:
        original_close(archive, source)
        closed.append((archive, source))
        raise fatal

    monkeypatch.setattr(zip_store, "_close_archive_source", close_then_fail)
    store = ZipOriginalStore(tmp_path)

    with pytest.raises(AcquisitionCorruptError) as raised:
        await store.open(_target())

    assert raised.value.__cause__ is fatal
    assert closed[0][0] is not None and closed[0][0].fp is None
    assert closed[0][1] is not None and closed[0][1].closed
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_malformed_zip64_outranks_wrapped_disappearance_during_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class DisappearingCloseSource:
        def __init__(self, source: BinaryIO) -> None:
            self._source = source

        def close(self) -> None:
            self._source.close()
            raise FileNotFoundError("archive disappeared during cleanup")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._source, name)

    archive_path = tmp_path / "books.zip"
    info = zipfile.ZipInfo("original.fb2")
    info.extra = struct.pack("<HH", 0x0001, 4) + b"\0" * 4
    _zip(archive_path, [(info, b"content")])
    payload = bytearray(archive_path.read_bytes())
    central = payload.index(b"PK\x01\x02")
    payload[central + 24 : central + 28] = (0xFFFFFFFF).to_bytes(4, "little")
    archive_path.write_bytes(payload)
    original_open = zip_store._open_binary

    def open_with_disappearing_close(path: Path) -> BinaryIO:
        return cast(BinaryIO, DisappearingCloseSource(original_open(path)))

    monkeypatch.setattr(zip_store, "_open_binary", open_with_disappearing_close)
    store = ZipOriginalStore(tmp_path)

    with pytest.raises(AcquisitionCorruptError) as raised:
        await store.open(_target())

    assert isinstance(raised.value.__cause__, zipfile.BadZipFile)
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_streams_original_in_bounded_chunks_and_releases_admission(tmp_path: Path) -> None:
    body = b"x" * (CHUNK_SIZE * 2 + 17)
    _zip(tmp_path / "books.zip", [("original.fb2", body)])
    store = ZipOriginalStore(tmp_path)

    first = await store.open(_target(size=len(body)))
    chunks = [chunk async for chunk in first]
    second = await store.open(_target(size=len(body)))
    await second.aclose()
    await store.shutdown()

    assert b"".join(chunks) == body
    assert [len(chunk) for chunk in chunks] == [CHUNK_SIZE, CHUNK_SIZE, 17]
    _assert_no_extraction(tmp_path)


@pytest.mark.parametrize(
    "archive",
    [
        "",
        "/books.zip",
        "../books.zip",
        "nested/../books.zip",
        "C:/books.zip",
        "a\\b.zip",
        "bad\n.zip",
    ],
)
async def test_rejects_unsafe_archive_paths(tmp_path: Path, archive: str) -> None:
    store = ZipOriginalStore(tmp_path)
    with pytest.raises(AcquisitionUnsafePathError):
        await store.open(_target(archive=archive))
    await store.shutdown()


@pytest.mark.parametrize(
    "member", ["", "/book.fb2", "../book.fb2", "C:/book.fb2", "a\\b.fb2", "bad\r.fb2"]
)
async def test_rejects_unsafe_member_paths(tmp_path: Path, member: str) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    with pytest.raises(AcquisitionUnsafePathError):
        await store.open(_target(member=member))
    await store.shutdown()


async def test_allows_in_root_symlink_chain_and_rejects_escape(tmp_path: Path) -> None:
    inside = tmp_path / "inside"
    inside.mkdir()
    _zip(inside / "books.zip", [("original.fb2", b"content")])
    (tmp_path / "safe.zip").symlink_to(inside / "books.zip")
    outside = tmp_path.parent / f"{tmp_path.name}-outside.zip"
    _zip(outside, [("original.fb2", b"content")])
    (tmp_path / "escape.zip").symlink_to(outside)
    store = ZipOriginalStore(tmp_path)

    stream = await store.open(_target(archive="safe.zip"))
    assert await _body(stream) == b"content"
    with pytest.raises(AcquisitionUnsafePathError):
        await store.open(_target(archive="escape.zip"))
    await store.shutdown()


async def test_rejects_archive_replaced_by_outside_symlink_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = tmp_path / "books.zip"
    _zip(archive_path, [("original.fb2", b"content")])
    outside = tmp_path.parent / f"{tmp_path.name}-replacement.zip"
    _zip(outside, [("original.fb2", b"content")])
    original_open = zip_store._open_binary

    def replace_then_open(path: Path) -> io.BufferedReader:
        path.unlink()
        path.symlink_to(outside)
        return original_open(path)  # type: ignore[return-value]

    monkeypatch.setattr(zip_store, "_open_binary", replace_then_open)
    store = ZipOriginalStore(tmp_path)
    try:
        with pytest.raises(AcquisitionUnsafePathError):
            await store.open(_target())
        await store.shutdown()
    finally:
        outside.unlink()


@pytest.mark.parametrize("error", [FileNotFoundError(), NotADirectoryError()])
async def test_disappearance_between_resolution_and_open_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])

    def disappear(_path: Path) -> io.BufferedReader:
        raise error

    monkeypatch.setattr(zip_store, "_open_binary", disappear)
    store = ZipOriginalStore(tmp_path)
    with pytest.raises(AcquisitionUnavailableError):
        await store.open(_target())
    await store.shutdown()


@pytest.mark.parametrize("error", [PermissionError("denied"), OSError("I/O failed")])
async def test_archive_open_operational_failures_are_source_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])

    def fail_open(_path: Path) -> io.BufferedReader:
        raise error

    monkeypatch.setattr(zip_store, "_open_binary", fail_open)
    store = ZipOriginalStore(tmp_path)
    with pytest.raises(AcquisitionSourceIOError):
        await store.open(_target())
    await store.shutdown()


async def test_fifo_archive_fails_promptly_without_consuming_admission_or_worker(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "books.zip"
    os.mkfifo(archive_path)
    store = ZipOriginalStore(tmp_path)

    with pytest.raises(AcquisitionUnsafePathError):
        await asyncio.wait_for(store.open(_target()), timeout=1)
    assert store._admission._value == MAX_OPEN_STREAMS

    archive_path.unlink()
    _zip(archive_path, [("original.fb2", b"content")])
    replacement = await asyncio.wait_for(store.open(_target()), timeout=1)
    await replacement.aclose()
    await store.shutdown()


async def test_directory_archive_is_rejected_as_unsafe(tmp_path: Path) -> None:
    (tmp_path / "books.zip").mkdir()
    store = ZipOriginalStore(tmp_path)

    with pytest.raises(AcquisitionUnsafePathError):
        await asyncio.wait_for(store.open(_target()), timeout=1)
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_exact_member_selection_missing_duplicate_directory_and_symlink(
    tmp_path: Path,
) -> None:
    store = ZipOriginalStore(tmp_path)
    _zip(tmp_path / "books.zip", [("other.fb2", b"content")])
    with pytest.raises(AcquisitionMemberNotFoundError):
        await store.open(_target())

    with pytest.warns(UserWarning):
        _zip(
            tmp_path / "books.zip",
            [("original.fb2", b"content"), ("original.fb2", b"content")],
        )
    with pytest.raises(AcquisitionAmbiguousMemberError):
        await store.open(_target())

    directory = zipfile.ZipInfo("original.fb2")
    directory.create_system = 3
    directory.external_attr = (stat.S_IFDIR | 0o755) << 16
    _zip(tmp_path / "books.zip", [(directory, b"")])
    with pytest.raises(AcquisitionDirectoryMemberError):
        await store.open(_target(size=0))

    dos_directory = zipfile.ZipInfo("original.fb2")
    dos_directory.create_system = 0
    dos_directory.external_attr = 0x10
    _zip(tmp_path / "books.zip", [(dos_directory, b"")])
    with pytest.raises(AcquisitionDirectoryMemberError):
        await store.open(_target(size=0))

    symlink = zipfile.ZipInfo("original.fb2")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    _zip(tmp_path / "books.zip", [(symlink, b"destination")])
    with pytest.raises(AcquisitionSymlinkMemberError):
        await store.open(_target(size=11))
    await store.shutdown()


async def test_encrypted_member_is_rejected_before_open(tmp_path: Path) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    payload = bytearray((tmp_path / "books.zip").read_bytes())
    local = payload.index(b"PK\x03\x04")
    central = payload.index(b"PK\x01\x02")
    payload[local + 6 : local + 8] = (1).to_bytes(2, "little")
    payload[central + 8 : central + 10] = (1).to_bytes(2, "little")
    (tmp_path / "books.zip").write_bytes(payload)
    store = ZipOriginalStore(tmp_path)

    with pytest.raises(AcquisitionEncryptedMemberError):
        await store.open(_target())
    await store.shutdown()


async def test_crc_corruption_is_detected_while_streaming_and_cleans_up(tmp_path: Path) -> None:
    with zipfile.ZipFile(tmp_path / "books.zip", "w", zipfile.ZIP_STORED) as archive:
        archive.writestr("original.fb2", b"content")
    payload = bytearray((tmp_path / "books.zip").read_bytes())
    body_offset = payload.index(b"content")
    payload[body_offset] ^= 0xFF
    (tmp_path / "books.zip").write_bytes(payload)
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())

    with pytest.raises(AcquisitionCorruptError):
        await _body(stream)
    replacement = await store.open(_target())
    await replacement.aclose()
    await store.shutdown()


async def test_size_malformed_and_truncated_archives_are_typed(tmp_path: Path) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    with pytest.raises(AcquisitionSizeMismatchError):
        await store.open(_target(size=8))

    (tmp_path / "books.zip").write_bytes(b"not a zip")
    with pytest.raises(AcquisitionCorruptError):
        await store.open(_target())

    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    payload = (tmp_path / "books.zip").read_bytes()
    (tmp_path / "books.zip").write_bytes(payload[:-10])
    with pytest.raises(AcquisitionCorruptError):
        await store.open(_target())
    await store.shutdown()


async def test_corrupt_deflate_decoder_error_is_typed_and_cleans_up(tmp_path: Path) -> None:
    body = b"highly compressible content" * 100
    _zip(tmp_path / "books.zip", [("original.fb2", body)])
    archive_path = tmp_path / "books.zip"
    payload = bytearray(archive_path.read_bytes())
    name_size, extra_size = struct.unpack("<HH", payload[26:30])
    compressed_offset = 30 + name_size + extra_size
    payload[compressed_offset] = 0xFF
    archive_path.write_bytes(payload)
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target(size=len(body)))

    with pytest.raises(AcquisitionCorruptError) as raised:
        await _body(stream)
    assert isinstance(raised.value.__cause__, zlib.error)
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_corrupt_bzip2_decoder_error_is_typed_and_cleans_up(tmp_path: Path) -> None:
    body = b"highly compressible content" * 100
    archive_path = tmp_path / "books.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_BZIP2) as archive:
        archive.writestr("original.fb2", body)
    payload = bytearray(archive_path.read_bytes())
    name_size, extra_size = struct.unpack("<HH", payload[26:30])
    compressed_offset = 30 + name_size + extra_size
    payload[compressed_offset + 4] ^= 0xFF
    archive_path.write_bytes(payload)
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target(size=len(body)))

    with pytest.raises(AcquisitionCorruptError) as raised:
        await _body(stream)
    assert type(raised.value.__cause__) is OSError
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_unmarked_member_read_oserror_is_corrupt_and_releases_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())

    def fail_read(_opened: _OpenedMember) -> bytes:
        raise OSError("synthetic decoder failure")

    monkeypatch.setattr(_OpenedMember, "read", fail_read)
    with pytest.raises(AcquisitionCorruptError):
        await _body(stream)
    await store.shutdown()


@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (FileNotFoundError("disappeared while reading"), AcquisitionUnavailableError),
        (NotADirectoryError("path disappeared while reading"), AcquisitionUnavailableError),
        (PermissionError("denied while reading"), AcquisitionSourceIOError),
        (OSError("source read failed"), AcquisitionSourceIOError),
    ],
)
async def test_wrapped_source_file_read_oserror_is_classified(
    tmp_path: Path,
    error: OSError,
    expected_error: type[Exception],
) -> None:
    class FailingReadSource:
        def __init__(self, source: BinaryIO, error: OSError) -> None:
            self._source = source
            self._error = error

        def read(self, _size: int = -1) -> bytes:
            raise self._error

        def __getattr__(self, name: str) -> Any:
            return getattr(self._source, name)

    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())
    opened = cast(zip_store._ZipMemberStream, stream)._opened
    source = cast(zip_store._SourceFile, opened.source)
    source._source = cast(BinaryIO, FailingReadSource(source._source, error))

    with pytest.raises(expected_error) as raised:
        await _body(stream)

    assert raised.value.__cause__ is error
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


@pytest.mark.parametrize(
    ("read_error", "close_error", "expected_error", "expected_cause"),
    [
        (
            FileNotFoundError("disappeared while reading"),
            OSError("close I/O failed"),
            AcquisitionCorruptError,
            "close",
        ),
        (
            OSError("decoder failed"),
            FileNotFoundError("disappeared while closing"),
            AcquisitionCorruptError,
            "read",
        ),
        (
            ValueError("invalid member data"),
            FileNotFoundError("disappeared while closing"),
            AcquisitionCorruptError,
            "read",
        ),
    ],
)
async def test_read_and_close_failure_precedence_releases_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    read_error: BaseException,
    close_error: BaseException,
    expected_error: type[Exception],
    expected_cause: str,
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())
    opened = cast(zip_store._ZipMemberStream, stream)._opened
    original_close = _OpenedMember.close

    def fail_read(_opened: _OpenedMember) -> bytes:
        raise read_error

    def close_then_fail(opened_member: _OpenedMember) -> None:
        original_close(opened_member)
        raise close_error

    monkeypatch.setattr(_OpenedMember, "read", fail_read)
    monkeypatch.setattr(_OpenedMember, "close", close_then_fail)

    with pytest.raises(expected_error) as raised:
        await _body(stream)

    cause = close_error if expected_cause == "close" else read_error
    assert raised.value.__cause__ is cause
    assert opened.member.closed and opened.source.closed and opened.archive.fp is None
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


@pytest.mark.parametrize(
    "close_error",
    [FileNotFoundError("disappeared while closing"), OSError("close I/O failed")],
)
async def test_cancellation_outranks_close_failure_and_releases_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    close_error: OSError,
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())
    opened = cast(zip_store._ZipMemberStream, stream)._opened
    entered = threading.Event()
    release = threading.Event()
    original_close = _OpenedMember.close

    def blocked_read(_opened: _OpenedMember) -> bytes:
        entered.set()
        release.wait(timeout=2)
        return b"content"

    def close_then_fail(opened_member: _OpenedMember) -> None:
        original_close(opened_member)
        raise close_error

    monkeypatch.setattr(_OpenedMember, "read", blocked_read)
    monkeypatch.setattr(_OpenedMember, "close", close_then_fail)
    consuming = asyncio.create_task(_body(stream))
    assert await asyncio.to_thread(entered.wait, 1)
    consuming.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await consuming

    assert opened.member.closed and opened.source.closed and opened.archive.fp is None
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_read_worker_failure_after_cancellation_does_not_replace_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())
    opened = cast(zip_store._ZipMemberStream, stream)._opened
    entered = threading.Event()
    release = threading.Event()

    def fail_after_release(_opened: _OpenedMember) -> bytes:
        entered.set()
        release.wait(timeout=2)
        raise OSError("decoder failed after cancellation")

    monkeypatch.setattr(_OpenedMember, "read", fail_after_release)
    consuming = asyncio.create_task(_body(stream))
    assert await asyncio.to_thread(entered.wait, 1)
    consuming.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await consuming

    assert opened.member.closed and opened.source.closed and opened.archive.fp is None
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_close_worker_failure_after_cancellation_does_not_replace_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())
    opened = cast(zip_store._ZipMemberStream, stream)._opened
    entered = threading.Event()
    release = threading.Event()
    original_close = _OpenedMember.close

    def fail_after_release(opened_member: _OpenedMember) -> None:
        entered.set()
        release.wait(timeout=2)
        original_close(opened_member)
        raise OSError("close failed after cancellation")

    monkeypatch.setattr(_OpenedMember, "close", fail_after_release)
    closing = asyncio.create_task(stream.aclose())
    assert await asyncio.to_thread(entered.wait, 1)
    closing.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await closing

    assert opened.member.closed and opened.source.closed and opened.archive.fp is None
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_repeated_open_cancellation_closes_successful_worker_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    opened_results: list[_OpenedMember] = []
    original_open_member = zip_store._open_member

    def blocked_open(root: Path, target: AcquisitionTarget) -> _OpenedMember:
        entered.set()
        release.wait(timeout=2)
        opened = original_open_member(root, target)
        opened_results.append(opened)
        completed.set()
        return opened

    monkeypatch.setattr(zip_store, "_open_member", blocked_open)
    store = ZipOriginalStore(tmp_path)
    opening = asyncio.create_task(store.open(_target()))
    assert await asyncio.to_thread(entered.wait, 1)

    for _ in range(3):
        opening.cancel()
        await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, timeout=1)

    assert completed.is_set()
    assert len(opened_results) == 1
    opened = opened_results[0]
    assert opened.member.closed and opened.source.closed and opened.archive.fp is None
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_fifth_stream_waits_for_an_admission_slot(tmp_path: Path) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    streams = [await store.open(_target()) for _ in range(4)]
    fifth = asyncio.create_task(store.open(_target()))
    await asyncio.sleep(0.02)
    assert not fifth.done()

    await streams.pop().aclose()
    admitted = await asyncio.wait_for(fifth, timeout=1)
    await admitted.aclose()
    await asyncio.gather(*(stream.aclose() for stream in streams))
    await store.shutdown()


async def test_cancellation_after_registration_releases_exactly_one_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    registered = asyncio.Event()
    interposition = asyncio.Event()
    original_register = store._register_stream

    async def register_then_wait(stream: zip_store._ZipMemberStream) -> bool:
        result = await original_register(stream)
        registered.set()
        await interposition.wait()
        return result

    monkeypatch.setattr(store, "_register_stream", register_then_wait)
    opening = asyncio.create_task(store.open(_target()))
    await registered.wait()
    opening.cancel()
    with pytest.raises(asyncio.CancelledError):
        await opening

    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    monkeypatch.setattr(store, "_register_stream", original_register)
    streams = [await store.open(_target()) for _ in range(MAX_OPEN_STREAMS)]
    fifth = asyncio.create_task(store.open(_target()))
    await asyncio.sleep(0)
    assert not fifth.done()
    await streams.pop().aclose()
    replacement = await asyncio.wait_for(fifth, timeout=1)
    await replacement.aclose()
    await asyncio.gather(*(stream.aclose() for stream in streams))
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_open_cancellation_during_handoff_closes_registered_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    registered = asyncio.Event()
    continue_after_registration = asyncio.Event()
    bookkeeping_started = asyncio.Event()
    streams: list[zip_store._ZipMemberStream] = []
    original_register = store._register_stream
    original_complete = store._complete_opening

    async def register_then_pause(stream: zip_store._ZipMemberStream) -> bool:
        result = await original_register(stream)
        streams.append(stream)
        registered.set()
        await continue_after_registration.wait()
        return result

    async def observe_completion(token: asyncio.Future[None]) -> None:
        bookkeeping_started.set()
        await original_complete(token)

    monkeypatch.setattr(store, "_register_stream", register_then_pause)
    monkeypatch.setattr(store, "_complete_opening", observe_completion)
    opening = asyncio.create_task(store.open(_target()))
    await registered.wait()
    await store._state_lock.acquire()
    continue_after_registration.set()
    await bookkeeping_started.wait()

    for _ in range(3):
        opening.cancel()
        await asyncio.sleep(0)
    assert not opening.done()
    store._state_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(opening, timeout=1)

    assert len(streams) == 1
    opened = streams[0]._opened
    assert opened.member.closed and opened.source.closed and opened.archive.fp is None
    assert not store._opening
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_describe_cancellation_during_bookkeeping_releases_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    worker_entered = threading.Event()
    release_worker = threading.Event()
    worker_completed = threading.Event()
    bookkeeping_started = asyncio.Event()
    original_describe = zip_store._describe_member
    original_finish = store._finish_description

    def controlled_describe(root: Path, target: AcquisitionTarget) -> SourceRevision:
        worker_entered.set()
        release_worker.wait(timeout=2)
        try:
            return original_describe(root, target)
        finally:
            worker_completed.set()

    async def observe_finish(token: asyncio.Future[None] | None) -> None:
        bookkeeping_started.set()
        await original_finish(token)

    monkeypatch.setattr(zip_store, "_describe_member", controlled_describe)
    monkeypatch.setattr(store, "_finish_description", observe_finish)
    describing = asyncio.create_task(store.describe(_target()))
    assert await asyncio.to_thread(worker_entered.wait, 1)
    await store._state_lock.acquire()
    release_worker.set()
    assert await asyncio.to_thread(worker_completed.wait, 1)
    await bookkeeping_started.wait()

    for _ in range(3):
        describing.cancel()
        await asyncio.sleep(0)
    assert not describing.done()
    store._state_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(describing, timeout=1)

    assert not store._opening
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_aclose_cancellation_after_physical_close_finishes_release_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = cast(zip_store._ZipMemberStream, await store.open(_target()))
    opened = stream._opened
    close_entered = threading.Event()
    release_close = threading.Event()
    physical_close_completed = threading.Event()
    original_close = opened.close

    def controlled_close() -> None:
        close_entered.set()
        release_close.wait(timeout=2)
        original_close()
        physical_close_completed.set()

    monkeypatch.setattr(opened, "close", controlled_close)
    closing = asyncio.create_task(stream.aclose())
    assert await asyncio.to_thread(close_entered.wait, 1)
    await store._state_lock.acquire()
    release_close.set()
    assert await asyncio.to_thread(physical_close_completed.wait, 1)

    for _ in range(3):
        closing.cancel()
        await asyncio.sleep(0)
    assert not closing.done()
    assert stream in store._streams
    store._state_lock.release()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(closing, timeout=1)
    await asyncio.wait_for(stream.aclose(), timeout=1)

    assert stream._closed
    assert opened.member.closed and opened.source.closed and opened.archive.fp is None
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


async def test_shutdown_closes_streams_and_rejects_admission(tmp_path: Path) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())

    await store.shutdown()

    await stream.aclose()
    with pytest.raises(AcquisitionStoreShutdownError):
        await store.open(_target())


async def test_open_error_caller_can_await_shutdown_without_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    worker_entered = threading.Event()
    release_worker = threading.Event()
    shutdown_started = asyncio.Event()
    error_caught = asyncio.Event()
    opened_results: list[_OpenedMember] = []
    original_open = zip_store._open_member
    original_run_shutdown = store._run_shutdown

    def controlled_open(root: Path, target: AcquisitionTarget) -> _OpenedMember:
        worker_entered.set()
        release_worker.wait(timeout=2)
        opened = original_open(root, target)
        opened_results.append(opened)
        return opened

    async def observed_shutdown() -> None:
        shutdown_started.set()
        await original_run_shutdown()

    async def open_then_shutdown() -> None:
        try:
            await store.open(_target())
        except AcquisitionStoreShutdownError:
            error_caught.set()
            await store.shutdown()
        else:
            raise AssertionError("Open unexpectedly succeeded during shutdown")

    monkeypatch.setattr(zip_store, "_open_member", controlled_open)
    monkeypatch.setattr(store, "_run_shutdown", observed_shutdown)
    caller = asyncio.create_task(open_then_shutdown())
    assert await asyncio.to_thread(worker_entered.wait, 1)
    shutdown = asyncio.create_task(store.shutdown())
    await shutdown_started.wait()
    release_worker.set()

    await asyncio.wait_for(asyncio.gather(caller, shutdown), timeout=1)

    assert error_caught.is_set()
    assert len(opened_results) == 1
    opened = opened_results[0]
    assert opened.member.closed and opened.source.closed and opened.archive.fp is None
    assert not store._opening
    assert not store._streams
    assert store._admission._value == MAX_OPEN_STREAMS


async def test_describe_error_caller_can_await_shutdown_without_deadlock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ZipOriginalStore(tmp_path)
    worker_entered = threading.Event()
    release_worker = threading.Event()
    shutdown_started = asyncio.Event()
    error_caught = asyncio.Event()
    original_run_shutdown = store._run_shutdown

    def controlled_describe(_root: Path, _target_value: AcquisitionTarget) -> SourceRevision:
        worker_entered.set()
        release_worker.wait(timeout=2)
        raise AcquisitionUnavailableError("Original archive is unavailable")

    async def observed_shutdown() -> None:
        shutdown_started.set()
        await original_run_shutdown()

    async def describe_then_shutdown() -> None:
        try:
            await store.describe(_target())
        except AcquisitionUnavailableError:
            error_caught.set()
            await store.shutdown()
        else:
            raise AssertionError("Description unexpectedly succeeded")

    monkeypatch.setattr(zip_store, "_describe_member", controlled_describe)
    monkeypatch.setattr(store, "_run_shutdown", observed_shutdown)
    caller = asyncio.create_task(describe_then_shutdown())
    assert await asyncio.to_thread(worker_entered.wait, 1)
    shutdown = asyncio.create_task(store.shutdown())
    await shutdown_started.wait()
    release_worker.set()

    await asyncio.wait_for(asyncio.gather(caller, shutdown), timeout=1)

    assert error_caught.is_set()
    assert not store._opening
    assert store._admission._value == MAX_OPEN_STREAMS


async def test_cancelled_first_shutdown_does_not_cancel_executor_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ZipOriginalStore(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    original_shutdown = store._executor.shutdown

    def controlled_shutdown(*, wait: bool, cancel_futures: bool) -> None:
        entered.set()
        release.wait(timeout=2)
        original_shutdown(wait=wait, cancel_futures=cancel_futures)
        completed.set()

    monkeypatch.setattr(store._executor, "shutdown", controlled_shutdown)
    first = asyncio.create_task(store.shutdown())
    assert await asyncio.to_thread(entered.wait, 1)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not completed.is_set()

    second = asyncio.create_task(store.shutdown())
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await asyncio.wait_for(second, timeout=1)
    assert completed.is_set()


async def test_cancellation_closes_stream_and_releases_slot(tmp_path: Path) -> None:
    body = b"x" * (CHUNK_SIZE * 3)
    _zip(tmp_path / "books.zip", [("original.fb2", body)])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target(size=len(body)))

    started = asyncio.Event()

    async def consume() -> None:
        async for _chunk in stream:
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    replacements = [await store.open(_target(size=len(body))) for _ in range(4)]
    await asyncio.gather(*(replacement.aclose() for replacement in replacements))
    await store.shutdown()


def test_media_types_safe_names_and_content_disposition() -> None:
    assert media_type_for("FB2") == "application/x-fictionbook+xml"
    assert media_type_for("epub") == "application/epub+zip"
    assert media_type_for("mobi") == "application/x-mobipocket-ebook"
    assert media_type_for("azw3") == "application/vnd.amazon.ebook"
    assert media_type_for("pdf") == "application/pdf"
    assert media_type_for("djvu") == "image/vnd.djvu"
    assert media_type_for("txt") == "text/plain; charset=utf-8"
    assert media_type_for("unknown") == "application/octet-stream"

    filename = safe_download_filename('  Книга/"bad"\r\n  ', ".fb2")
    assert filename == "Книга__bad___.fb2"
    header = content_disposition(filename)
    assert "\r" not in header and "\n" not in header
    assert 'filename="__bad___.fb2"' in header
    assert "filename*=UTF-8''%D0%9A%D0%BD%D0%B8%D0%B3%D0%B0" in header
    assert content_disposition("Книга.fb2").startswith('attachment; filename="book.fb2";')
    controlled = safe_download_filename("safe\u202etxt\u2066", "fb2")
    assert controlled == "safe_txt_.fb2"
    controlled_header = content_disposition("safe\u202etxt.fb2")
    assert "\u202e" not in controlled_header
    assert "%E2%80%AE" not in controlled_header
    assert safe_download_filename("..", "") == "book.bin"
