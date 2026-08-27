"""Conversion contracts, registry, cache, and orchestration tests."""

import asyncio
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from sopds.acquisition.contracts import (
    AcquiredOriginal,
    AsyncByteStream,
    OriginalDescription,
    SourceRevision,
)
from sopds.conversion.cache import CACHE_CHUNK_SIZE, ArtifactCache, cache_digest
from sopds.conversion.contracts import (
    ConversionCapability,
    ConversionShutdownError,
    ConversionSourceKey,
    ConverterIdentity,
    InvalidConversionOutputError,
    SourceChangedError,
    UnsupportedConversionError,
)
from sopds.conversion.registry import ConverterRegistry
from sopds.conversion.service import ConversionService

_REVISION = SourceRevision(100, 200, 300)
_CAPABILITY = ConversionCapability("FB2", ".EPUB", "Application/EPUB+ZIP", ".epub")


class _BytesStream:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False
        self._used = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        if self._used:
            raise RuntimeError
        self._used = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        yield self.body

    async def aclose(self) -> None:
        self.closed = True


class _Converter:
    def __init__(self, *, gate: asyncio.Event | None = None, fail: bool = False) -> None:
        self._identity = ConverterIdentity("fake", "1.0")
        self._capabilities = (_CAPABILITY,)
        self.gate = gate
        self.fail = fail
        self.calls = 0
        self.started = asyncio.Event()
        self.source_sizes: list[int] = []

    @property
    def identity(self) -> ConverterIdentity:
        return self._identity

    @property
    def capabilities(self) -> tuple[ConversionCapability, ...]:
        return self._capabilities

    async def convert(self, source_path: Path, target_format: str, output_path: Path) -> None:
        self.calls += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("private converter detail")
        self.source_sizes.append((await asyncio.to_thread(source_path.stat)).st_size)
        await asyncio.to_thread(output_path.write_bytes, b"converted")


class _Acquisition:
    def __init__(self, revision: SourceRevision = _REVISION, body: bytes = b"source") -> None:
        self.revision = revision
        self.observed_revision = revision
        self.body = body
        self.streams: list[_BytesStream] = []

    async def describe(self, public_id: str) -> OriginalDescription:
        return OriginalDescription(
            public_id, "Unsafe / title", "FB2", len(self.body), self.revision
        )

    async def acquire(self, public_id: str) -> AcquiredOriginal:
        stream = _BytesStream(self.body)
        self.streams.append(stream)
        return AcquiredOriginal(
            "book.fb2",
            "application/x-fictionbook+xml",
            len(self.body),
            stream,
            "fb2",
            self.observed_revision,
        )


def _key() -> ConversionSourceKey:
    return ConversionSourceKey("public", _REVISION, "FB2", ".epub", ConverterIdentity("fake", "1"))


async def _read(stream: AsyncByteStream) -> bytes:
    return b"".join([chunk async for chunk in stream])


def test_registry_is_empty_and_normalizes_without_execution() -> None:
    converter = _Converter()
    empty = ConverterRegistry()
    registry = ConverterRegistry([converter])

    assert len(empty) == 0
    assert empty.capabilities() == ()
    assert registry.resolve(" .FB2 ", "EPUB").converter is converter
    assert registry.capabilities() == (_CAPABILITY,)
    assert converter.calls == 0
    with pytest.raises(UnsupportedConversionError):
        empty.resolve("fb2", "epub")
    with pytest.raises(ValueError):
        registry.resolve("../fb2", "epub")
    with pytest.raises(ValueError):
        ConverterRegistry([converter, _Converter()])


def test_cache_digest_is_deterministic_and_path_free() -> None:
    digest = cache_digest(_key())

    assert digest == cache_digest(_key())
    assert len(digest) == 64
    assert "public" not in digest
    changed = ConversionSourceKey(
        "public",
        SourceRevision(101, 200, 300),
        "fb2",
        "epub",
        ConverterIdentity("fake", "1"),
    )
    assert cache_digest(changed) != digest


async def test_cache_restart_hit_and_invalid_output_retry(tmp_path: Path) -> None:
    calls = 0

    async def invalid(_output: Path) -> None:
        nonlocal calls
        calls += 1

    first = ArtifactCache(tmp_path, 60)
    await first.startup()
    with pytest.raises(InvalidConversionOutputError):
        await first.get_or_create(_key(), invalid)

    async def valid(output: Path) -> None:
        nonlocal calls
        calls += 1
        await asyncio.to_thread(output.write_bytes, b"artifact")

    artifact = await first.get_or_create(_key(), valid)
    assert await _read(artifact.stream) == b"artifact"
    await first.shutdown()

    second = ArtifactCache(tmp_path, 60)
    await second.startup()

    async def must_not_run(_output: Path) -> None:
        raise AssertionError

    hit = await second.get_or_create(_key(), must_not_run)
    assert await _read(hit.stream) == b"artifact"
    assert calls == 2
    await second.shutdown()


async def test_cache_single_flight_survives_requester_cancellation(tmp_path: Path) -> None:
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def produce(output: Path) -> None:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        await asyncio.to_thread(output.write_bytes, b"artifact")

    cancelled = asyncio.create_task(cache.get_or_create(_key(), produce))
    await started.wait()
    survivor = asyncio.create_task(cache.get_or_create(_key(), produce))
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    release.set()
    result = await survivor

    assert calls == 1
    assert await _read(result.stream) == b"artifact"
    await cache.shutdown()


async def test_cache_hard_ttl_and_startup_temp_recovery(tmp_path: Path) -> None:
    stale_source = tmp_path / f"{cache_digest(_key())}.dead.source"
    stale_output = tmp_path / f"{cache_digest(_key())}.dead.tmp"
    stale_source.write_bytes(b"x")
    stale_output.write_bytes(b"x")
    cache = ArtifactCache(tmp_path, 1)
    await cache.startup()
    assert not stale_source.exists()
    assert not stale_output.exists()
    calls = 0

    async def produce(output: Path) -> None:
        nonlocal calls
        calls += 1
        await asyncio.to_thread(output.write_bytes, str(calls).encode())

    first = await cache.get_or_create(_key(), produce)
    assert await _read(first.stream) == b"1"
    artifact_path = tmp_path / f"{cache_digest(_key())}.artifact"
    old = 1_000_000_000
    os.utime(artifact_path, ns=(old, old))
    second = await cache.get_or_create(_key(), produce)
    assert await _read(second.stream) == b"2"
    assert calls == 2
    await cache.shutdown()


async def test_cleanup_excludes_active_artifact_leases(tmp_path: Path) -> None:
    cache = ArtifactCache(tmp_path, 1)
    await cache.startup()

    async def produce(output: Path) -> None:
        await asyncio.to_thread(output.write_bytes, b"artifact")

    lease = await cache.get_or_create(_key(), produce)
    artifact_path = tmp_path / f"{cache_digest(_key())}.artifact"
    await asyncio.to_thread(os.utime, artifact_path, ns=(1_000_000_000, 1_000_000_000))
    await cache.cleanup()
    assert await asyncio.to_thread(artifact_path.exists)

    await lease.stream.aclose()
    await cache.cleanup()
    assert not await asyncio.to_thread(artifact_path.exists)
    await cache.shutdown()


async def test_cleanup_counts_artifact_inspection_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = ArtifactCache(tmp_path, 1)
    await cache.startup()
    artifact_path = tmp_path / f"{cache_digest(_key())}.artifact"
    artifact_path.write_bytes(b"stale")
    real_stat = Path.stat

    def fail_target_stat(path: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if path == artifact_path and not follow_symlinks:
            raise PermissionError
        return real_stat(path, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", fail_target_stat)
    summary = await cache.cleanup()

    assert summary.removed_files == 0
    assert summary.failed_entries == 1
    await cache.shutdown()


async def test_service_spools_and_closes_source_and_sanitizes_name(tmp_path: Path) -> None:
    body = b"x" * (CACHE_CHUNK_SIZE * 3 + 7)
    acquisition = _Acquisition(body=body)
    converter = _Converter()
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(acquisition, ConverterRegistry([converter]), cache)

    result = await service.convert("public", ".EPUB")

    assert result.filename == "Unsafe _ title.epub"
    assert result.media_type == "application/epub+zip"
    assert result.content_length == len(b"converted")
    assert await _read(result.stream) == b"converted"
    assert converter.source_sizes == [len(body)]
    assert acquisition.streams[0].closed
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source")))
    await service.shutdown()


async def test_service_rejects_revision_race_before_converter(tmp_path: Path) -> None:
    acquisition = _Acquisition()
    acquisition.observed_revision = SourceRevision(101, 200, 300)
    converter = _Converter()
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(acquisition, ConverterRegistry([converter]), cache)

    with pytest.raises(SourceChangedError):
        await service.convert("public", "epub")

    assert converter.calls == 0
    assert acquisition.streams[0].closed
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source")))
    await service.shutdown()


async def test_cleanup_is_atomic_with_producer_and_source_registration(tmp_path: Path) -> None:
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    producer_started = asyncio.Event()
    producer_release = asyncio.Event()

    async def produce(output: Path) -> None:
        await asyncio.to_thread(output.write_bytes, b"partial")
        producer_started.set()
        await producer_release.wait()

    request = asyncio.create_task(cache.get_or_create(_key(), produce))
    await producer_started.wait()
    await cache.cleanup()
    assert await asyncio.to_thread(lambda: list(tmp_path.glob("*.tmp")))

    source = await cache.create_source_path(cache_digest(_key()))
    await cache.cleanup()
    assert source.exists()
    await cache.remove_source_path(source)
    producer_release.set()
    result = await request
    await result.stream.aclose()
    await cache.shutdown()


async def test_source_creation_cancellation_cleans_completed_mkstemp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    entered = threading.Event()
    release = threading.Event()
    real_mkstemp = tempfile.mkstemp

    def gated_mkstemp(
        *,
        prefix: str,
        suffix: str,
        dir: Path,  # noqa: A002
    ) -> tuple[int, str]:
        entered.set()
        assert release.wait(5)
        return real_mkstemp(prefix=prefix, suffix=suffix, dir=dir)

    monkeypatch.setattr(tempfile, "mkstemp", gated_mkstemp)
    creation = asyncio.create_task(cache.create_source_path(cache_digest(_key())))
    assert await asyncio.to_thread(entered.wait, 5)
    creation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await creation
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source")))
    await cache.shutdown()


@pytest.mark.parametrize("replacement", ["zero", "symlink", "fifo", "directory"])
async def test_invalid_cache_replacements_regenerate_without_blocking(
    tmp_path: Path, replacement: str
) -> None:
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    artifact_path = tmp_path / f"{cache_digest(_key())}.artifact"
    external = tmp_path / "external"
    external.write_bytes(b"external secret")
    if replacement == "zero":
        artifact_path.touch()
    elif replacement == "symlink":
        artifact_path.symlink_to(external)
    elif replacement == "fifo":
        os.mkfifo(artifact_path)
    else:
        artifact_path.mkdir()

    calls = 0

    async def produce(output: Path) -> None:
        nonlocal calls
        calls += 1
        await asyncio.to_thread(output.write_bytes, b"safe")

    result = await asyncio.wait_for(cache.get_or_create(_key(), produce), 2)
    assert await _read(result.stream) == b"safe"
    assert external.read_bytes() == b"external secret"
    assert calls == 1
    await cache.shutdown()


async def test_future_artifact_timestamp_is_regenerated(tmp_path: Path) -> None:
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    artifact_path = tmp_path / f"{cache_digest(_key())}.artifact"
    artifact_path.write_bytes(b"future")
    future = time.time_ns() + 60_000_000_000
    os.utime(artifact_path, ns=(future, future))

    async def produce(output: Path) -> None:
        await asyncio.to_thread(output.write_bytes, b"current")

    result = await cache.get_or_create(_key(), produce)
    assert await _read(result.stream) == b"current"
    await cache.shutdown()


async def test_shutdown_cancels_hung_producer_and_cleans_temps(tmp_path: Path) -> None:
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def produce(output: Path) -> None:
        await asyncio.to_thread(output.write_bytes, b"partial")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    request = asyncio.create_task(cache.get_or_create(_key(), produce))
    await started.wait()
    await asyncio.wait_for(cache.shutdown(), 2)
    assert cancelled.is_set()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.tmp")))
    with pytest.raises(asyncio.CancelledError):
        await request
    with pytest.raises(ConversionShutdownError):
        await cache.get_or_create(_key(), produce)
    await cache.shutdown()


async def test_service_shutdown_cancels_gated_converter_and_removes_source(tmp_path: Path) -> None:
    acquisition = _Acquisition()
    converter = _Converter(gate=asyncio.Event())
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(acquisition, ConverterRegistry([converter]), cache)
    request = asyncio.create_task(service.convert("public", "epub"))
    await converter.started.wait()

    await asyncio.wait_for(service.shutdown(), 2)

    with pytest.raises(asyncio.CancelledError):
        await request
    assert acquisition.streams[0].closed
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source")))


async def test_converter_directory_output_preserves_invalid_output_error(tmp_path: Path) -> None:
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()

    async def produce(output: Path) -> None:
        await asyncio.to_thread(output.mkdir)

    with pytest.raises(InvalidConversionOutputError, match="no valid output"):
        await cache.get_or_create(_key(), produce)
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.tmp")))
    await cache.shutdown()
