"""Original acquisition, streaming, and presentation tests."""

from __future__ import annotations

import asyncio
import io
import os
import stat
import struct
import threading
import zipfile
import zlib
from pathlib import Path

import pytest

from sopds.acquisition import zip_store
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
    AsyncByteStream,
)
from sopds.acquisition.service import (
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


def _zip(path: Path, members: list[tuple[str | zipfile.ZipInfo, bytes]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members:
            archive.writestr(name, body)


async def _body(stream: AsyncByteStream) -> bytes:
    chunks = [chunk async for chunk in stream]
    return b"".join(chunks)


def _assert_no_extraction(root: Path) -> None:
    assert not list(root.glob("**/original.fb2"))


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "member", ["", "/book.fb2", "../book.fb2", "C:/book.fb2", "a\\b.fb2", "bad\r.fb2"]
)
async def test_rejects_unsafe_member_paths(tmp_path: Path, member: str) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    with pytest.raises(AcquisitionUnsafePathError):
        await store.open(_target(member=member))
    await store.shutdown()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_disappearance_between_resolution_and_open_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])

    def disappear(path: Path) -> io.BufferedReader:
        path.unlink()
        raise FileNotFoundError

    monkeypatch.setattr(zip_store, "_open_binary", disappear)
    store = ZipOriginalStore(tmp_path)
    with pytest.raises(AcquisitionUnavailableError):
        await store.open(_target())
    await store.shutdown()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_directory_archive_is_rejected_as_unsafe(tmp_path: Path) -> None:
    (tmp_path / "books.zip").mkdir()
    store = ZipOriginalStore(tmp_path)

    with pytest.raises(AcquisitionUnsafePathError):
        await asyncio.wait_for(store.open(_target()), timeout=1)
    assert store._admission._value == MAX_OPEN_STREAMS
    await store.shutdown()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_read_failure_cleans_up_and_releases_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())

    def fail_read(_opened: _OpenedMember) -> bytes:
        raise OSError("synthetic read failure")

    monkeypatch.setattr(_OpenedMember, "read", fail_read)
    with pytest.raises(AcquisitionCorruptError):
        await _body(stream)
    await store.shutdown()


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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


@pytest.mark.asyncio
async def test_shutdown_closes_streams_and_rejects_admission(tmp_path: Path) -> None:
    _zip(tmp_path / "books.zip", [("original.fb2", b"content")])
    store = ZipOriginalStore(tmp_path)
    stream = await store.open(_target())

    await store.shutdown()

    await stream.aclose()
    with pytest.raises(AcquisitionStoreShutdownError):
        await store.open(_target())


@pytest.mark.asyncio
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


@pytest.mark.asyncio
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
