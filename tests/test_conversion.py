"""Conversion contracts, registry, cache, and orchestration tests."""

import asyncio
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import BinaryIO, cast, override

import pytest

from sopds.acquisition.contracts import (
    AcquiredOriginal,
    AcquisitionCorruptError,
    AcquisitionMemberNotFoundError,
    AsyncByteStream,
    OriginalDescription,
    SourceRevision,
)
from sopds.conversion import service as conversion_service
from sopds.conversion.adapters import EpubToAzw3Converter, Fb2ToEpubConverter
from sopds.conversion.cache import CACHE_CHUNK_SIZE, ArtifactCache, cache_digest
from sopds.conversion.contracts import (
    ConversionCapability,
    ConversionShutdownError,
    ConversionSourceError,
    ConversionSourceKey,
    ConverterExecutionError,
    ConverterIdentity,
    InvalidConversionOutputError,
    SourceChangedError,
    SourceUnavailableError,
    UnsupportedConversionError,
)
from sopds.conversion.policy import OUTPUT_POLICY, OutputDecision
from sopds.conversion.process import ProcessResult
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


class _CloseFailingFile:
    """Release a staging descriptor while simulating a close-time I/O failure."""

    def __init__(self, file: BinaryIO) -> None:
        self._file = file

    def write(self, data: bytes) -> int:
        return self._file.write(data)

    def flush(self) -> None:
        self._file.flush()

    def close(self) -> None:
        self._file.close()
        raise OSError("private staging path")


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
    def __init__(
        self,
        revision: SourceRevision = _REVISION,
        body: bytes = b"source",
        source_format: str = "fb2",
    ) -> None:
        self.revision = revision
        self.observed_revision = revision
        self.body = body
        self.source_format = source_format
        self.streams: list[_BytesStream] = []
        self.describe_generations: list[int | None] = []
        self.acquire_generations: list[int | None] = []

    async def describe(
        self,
        public_id: str,
        *,
        expected_generation_id: int | None = None,
    ) -> OriginalDescription:
        self.describe_generations.append(expected_generation_id)
        return OriginalDescription(
            public_id, "Unsafe / title", self.source_format, len(self.body), self.revision
        )

    async def acquire(
        self,
        public_id: str,
        *,
        expected_generation_id: int | None = None,
    ) -> AcquiredOriginal:
        del public_id
        self.acquire_generations.append(expected_generation_id)
        stream = _BytesStream(self.body)
        self.streams.append(stream)
        return AcquiredOriginal(
            "book.fb2",
            "application/x-fictionbook+xml",
            len(self.body),
            stream,
            self.source_format,
            self.observed_revision,
        )


def _key() -> ConversionSourceKey:
    return ConversionSourceKey("public", _REVISION, "FB2", ".epub", ConverterIdentity("fake", "1"))


async def _read(stream: AsyncByteStream) -> bytes:
    return b"".join([chunk async for chunk in stream])


def test_output_policy_represents_canonical_choices_and_decision_table() -> None:
    assert tuple(choice.key for choice in OUTPUT_POLICY.choices()) == ("original", "epub", "azw3")
    assert OUTPUT_POLICY.choice("EPUB").label == "EPUB"
    assert OUTPUT_POLICY.choice("azw3").media_type == "application/vnd.amazon.ebook"
    assert OUTPUT_POLICY.decision("fb2", "original") is OutputDecision.ORIGINAL
    assert OUTPUT_POLICY.decision("fb2", "epub") is OutputDecision.CONVERT
    assert OUTPUT_POLICY.decision("fb2", "azw3") is OutputDecision.CONVERT
    assert OUTPUT_POLICY.decision("epub", "epub") is OutputDecision.PASSTHROUGH
    assert OUTPUT_POLICY.decision("epub", "azw3") is OutputDecision.CONVERT
    assert OUTPUT_POLICY.decision("azw3", "azw3") is OutputDecision.PASSTHROUGH
    assert OUTPUT_POLICY.decision("azw3", "epub") is OutputDecision.UNSUPPORTED
    assert OUTPUT_POLICY.decision("pdf", "epub") is OutputDecision.UNSUPPORTED
    assert tuple(choice.key for choice in OUTPUT_POLICY.targets_for("epub")) == (
        "original",
        "epub",
        "azw3",
    )


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

    pass_through = _Converter()
    pass_through._capabilities = (
        ConversionCapability("epub", "epub", "application/epub+zip", "epub"),
    )
    with pytest.raises(ValueError, match="canonical output policy"):
        ConverterRegistry([pass_through])


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
    stale_canonical_source = tmp_path / f"{cache_digest(_key())}.dead.source.fb2"
    stale_output = tmp_path / f"{cache_digest(_key())}.dead.tmp"
    stale_source.write_bytes(b"x")
    stale_canonical_source.write_bytes(b"x")
    stale_output.write_bytes(b"x")
    cache = ArtifactCache(tmp_path, 1)
    await cache.startup()
    assert not stale_source.exists()
    assert not stale_canonical_source.exists()
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
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source*")))
    await service.shutdown()


@pytest.mark.parametrize(("source_format", "target_format"), [("fb2", "epub"), ("epub", "azw3")])
async def test_service_stages_source_with_canonical_adapter_extension(
    tmp_path: Path, source_format: str, target_format: str
) -> None:
    source_name = ""

    async def extension_sensitive_runner(argv: tuple[str, ...]) -> ProcessResult:
        nonlocal source_name
        source = Path(argv[-1] if source_format == "fb2" else argv[2])
        assert source.suffix == f".{source_format}"
        source_name = source.name
        raise ConverterExecutionError("Converter execution failed")

    converter = (
        Fb2ToEpubConverter(runner=extension_sensitive_runner)
        if source_format == "fb2"
        else EpubToAzw3Converter(runner=extension_sensitive_runner)
    )
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(
        _Acquisition(source_format=source_format), ConverterRegistry([converter]), cache
    )

    with pytest.raises(ConverterExecutionError):
        await service.convert("public", target_format)

    assert "public" not in source_name
    assert "book" not in source_name
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source*")))
    await service.shutdown()


@pytest.mark.parametrize("stage", ["create", "open", "close"])
async def test_source_staging_io_failures_remain_path_free_source_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    acquisition = _Acquisition()
    converter = _Converter()
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(acquisition, ConverterRegistry([converter]), cache)

    if stage == "create":
        real_close = os.close

        def fail_descriptor_close(descriptor: int) -> None:
            real_close(descriptor)
            raise OSError("private staging path")

        monkeypatch.setattr(os, "close", fail_descriptor_close)
    else:
        real_open = Path.open

        def staging_open(self: Path, mode: str = "r") -> BinaryIO:
            if ".source." in self.name and stage == "open":
                raise OSError("private staging path")
            opened = cast(BinaryIO, real_open(self, mode))
            if ".source." in self.name:
                return cast(BinaryIO, _CloseFailingFile(opened))
            return opened

        monkeypatch.setattr(Path, "open", staging_open)

    with pytest.raises(ConversionSourceError) as raised:
        await service.convert("public", "epub")

    assert "private staging path" not in str(raised.value)
    assert converter.calls == 0
    assert acquisition.streams[0].closed
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source*")))
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.tmp")))
    await service.shutdown()


async def test_source_staging_close_failure_does_not_replace_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class HangingStream:
        """Keep source staging active until cache shutdown cancels its producer."""

        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.closed = False

        def __aiter__(self) -> AsyncIterator[bytes]:
            return self._iterate()

        async def _iterate(self) -> AsyncIterator[bytes]:
            self.started.set()
            await asyncio.Event().wait()
            yield b"unreachable"

        async def aclose(self) -> None:
            self.closed = True

    class HangingAcquisition(_Acquisition):
        def __init__(self) -> None:
            super().__init__()
            self.hanging_stream = HangingStream()

        @override
        async def acquire(
            self, public_id: str, *, expected_generation_id: int | None = None
        ) -> AcquiredOriginal:
            del public_id, expected_generation_id
            return AcquiredOriginal(
                "book.fb2",
                "application/x-fictionbook+xml",
                len(self.body),
                self.hanging_stream,
                "fb2",
                self.observed_revision,
            )

    real_open = Path.open

    def staging_open(self: Path, mode: str = "r") -> BinaryIO:
        opened = cast(BinaryIO, real_open(self, mode))
        if ".source." in self.name:
            return cast(BinaryIO, _CloseFailingFile(opened))
        return opened

    monkeypatch.setattr(Path, "open", staging_open)
    acquisition = HangingAcquisition()
    converter = _Converter()
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(acquisition, ConverterRegistry([converter]), cache)
    request = asyncio.create_task(service.convert("public", "epub"))
    await acquisition.hanging_stream.started.wait()

    await service.shutdown()

    with pytest.raises(asyncio.CancelledError):
        await request
    assert acquisition.hanging_stream.closed
    assert converter.calls == 0
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source*")))


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
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source*")))
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

    source = await cache.create_source_path(cache_digest(_key()), "fb2")
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
    creation = asyncio.create_task(cache.create_source_path(cache_digest(_key()), "fb2"))
    assert await asyncio.to_thread(entered.wait, 5)
    creation.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await creation
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source*")))
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


async def test_shutdown_does_not_recancel_final_waiter_retirement(tmp_path: Path) -> None:
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    repeated_cancellation = asyncio.Event()

    async def produce(output: Path) -> None:
        await asyncio.to_thread(output.write_bytes, b"partial")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            try:
                await cleanup_release.wait()
            except asyncio.CancelledError:
                repeated_cancellation.set()
                raise

    request = asyncio.create_task(cache.get_or_create(_key(), produce))
    await started.wait()
    request.cancel()
    await cleanup_started.wait()
    shutdown = asyncio.create_task(cache.shutdown())
    await asyncio.sleep(0)

    assert not shutdown.done()
    assert not repeated_cancellation.is_set()
    cleanup_release.set()
    await asyncio.wait_for(shutdown, 2)

    with pytest.raises(asyncio.CancelledError):
        await request
    assert not repeated_cancellation.is_set()
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.tmp")))


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
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source*")))


async def test_service_sole_waiter_cancellation_stops_converter_and_cleans_working_files(
    tmp_path: Path,
) -> None:
    class GatedConverter(_Converter):
        def __init__(self) -> None:
            super().__init__()
            self.cleanup_started = asyncio.Event()
            self.cleanup_release = asyncio.Event()
            self.cancelled = asyncio.Event()

        @override
        async def convert(self, source_path: Path, target_format: str, output_path: Path) -> None:
            del source_path, target_format
            self.calls += 1
            await asyncio.to_thread(output_path.write_bytes, b"partial")
            self.started.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.cleanup_started.set()
                await self.cleanup_release.wait()
                self.cancelled.set()

    acquisition = _Acquisition()
    converter = GatedConverter()
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(acquisition, ConverterRegistry([converter]), cache)
    request = asyncio.create_task(service.convert("public", "epub"))
    await converter.started.wait()

    request.cancel()
    await converter.cleanup_started.wait()
    request.cancel()
    await asyncio.sleep(0)
    assert not request.done()
    converter.cleanup_release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(request, 2)
    assert converter.cancelled.is_set()
    assert acquisition.streams[0].closed
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source*")))
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.tmp")))
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.artifact")))
    await service.shutdown()


async def test_service_cancelled_waiter_preserves_same_key_producer_for_survivor(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    release = asyncio.Event()
    converter = _Converter(gate=release)
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(acquisition, ConverterRegistry([converter]), cache)
    cancelled = asyncio.create_task(service.convert("public", "epub"))
    await converter.started.wait()
    survivor = asyncio.create_task(service.convert("public", "epub"))
    await asyncio.sleep(0)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not survivor.done()
    release.set()
    result = await asyncio.wait_for(survivor, 2)

    assert converter.calls == 1
    assert await _read(result.stream) == b"converted"
    assert acquisition.streams[0].closed
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.source*")))
    await service.shutdown()


async def test_service_passes_expected_generation_to_describe_and_acquire(
    tmp_path: Path,
) -> None:
    acquisition = _Acquisition()
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(acquisition, ConverterRegistry([_Converter()]), cache)

    result = await service.convert("public", "epub", expected_generation_id=47)
    await result.stream.aclose()

    assert acquisition.describe_generations == [47]
    assert acquisition.acquire_generations == [47]
    await service.shutdown()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AcquisitionMemberNotFoundError("private"), SourceUnavailableError),
        (AcquisitionCorruptError("private"), ConversionSourceError),
    ],
)
@pytest.mark.parametrize("stage", ["describe", "acquire"])
async def test_service_distinguishes_unavailable_from_source_integrity_failures(
    tmp_path: Path,
    error: Exception,
    expected: type[Exception],
    stage: str,
) -> None:
    class FailingAcquisition(_Acquisition):
        @override
        async def describe(
            self, public_id: str, *, expected_generation_id: int | None = None
        ) -> OriginalDescription:
            if stage == "describe":
                raise error
            return await super().describe(public_id, expected_generation_id=expected_generation_id)

        @override
        async def acquire(
            self, public_id: str, *, expected_generation_id: int | None = None
        ) -> AcquiredOriginal:
            if stage == "acquire":
                raise error
            return await super().acquire(public_id, expected_generation_id=expected_generation_id)

    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(FailingAcquisition(), ConverterRegistry([_Converter()]), cache)

    with pytest.raises(expected) as raised:
        await service.convert("public", "epub")

    assert "private" not in str(raised.value)
    await service.shutdown()


async def test_service_limits_distinct_cache_misses_to_two_converter_jobs(
    tmp_path: Path,
) -> None:
    class CountingConverter(_Converter):
        def __init__(self) -> None:
            super().__init__()
            self.active = 0
            self.maximum_active = 0
            self.two_started = asyncio.Event()
            self.release = asyncio.Event()

        @override
        async def convert(self, source_path: Path, target_format: str, output_path: Path) -> None:
            del source_path, target_format
            self.calls += 1
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
            if self.active == 2:
                self.two_started.set()
            try:
                await self.release.wait()
                await asyncio.to_thread(output_path.write_bytes, b"converted")
            finally:
                self.active -= 1

    acquisition = _Acquisition()
    converter = CountingConverter()
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()
    service = ConversionService(acquisition, ConverterRegistry([converter]), cache)
    converter.release.set()
    cached = await service.convert("cached", "epub")
    await cached.stream.aclose()
    converter.release.clear()
    requests = [
        asyncio.create_task(service.convert(f"public-{index}", "epub")) for index in range(3)
    ]

    await asyncio.wait_for(converter.two_started.wait(), 2)
    await asyncio.sleep(0)
    assert converter.calls == 3
    cache_hit = await asyncio.wait_for(service.convert("cached", "epub"), 0.5)
    await cache_hit.stream.aclose()
    assert converter.calls == 3
    converter.release.set()
    results = await asyncio.gather(*requests)

    assert converter.calls == 4
    assert converter.maximum_active == 2
    await asyncio.gather(*(result.stream.aclose() for result in results))
    await service.shutdown()


async def test_process_wide_limit_does_not_leak_cancelled_waiter() -> None:
    release = asyncio.Event()
    two_started = asyncio.Event()
    active = 0

    async def hold_slot() -> None:
        nonlocal active
        async with conversion_service._CONVERSION_SLOTS.slot():
            active += 1
            if active == 2:
                two_started.set()
            try:
                await release.wait()
            finally:
                active -= 1

    holders = [asyncio.create_task(hold_slot()) for _ in range(2)]
    await asyncio.wait_for(two_started.wait(), 2)
    waiter = asyncio.create_task(hold_slot())
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    release.set()
    await asyncio.gather(*holders)

    reacquired = 0
    both_reacquired = asyncio.Event()
    release_reacquired = asyncio.Event()

    async def reacquire_slot() -> None:
        nonlocal reacquired
        async with conversion_service._CONVERSION_SLOTS.slot():
            reacquired += 1
            if reacquired == 2:
                both_reacquired.set()
            await release_reacquired.wait()

    replacements = [asyncio.create_task(reacquire_slot()) for _ in range(2)]
    await asyncio.wait_for(both_reacquired.wait(), 0.5)
    release_reacquired.set()
    await asyncio.gather(*replacements)


def test_service_limit_survives_sequential_event_loop_lifecycles(tmp_path: Path) -> None:
    async def exercise(cache_path: Path) -> None:
        class GatedConverter(_Converter):
            def __init__(self) -> None:
                super().__init__()
                self.active = 0
                self.maximum_active = 0
                self.two_started = asyncio.Event()
                self.release = asyncio.Event()

            @override
            async def convert(
                self, source_path: Path, target_format: str, output_path: Path
            ) -> None:
                del source_path, target_format
                self.calls += 1
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                if self.active == 2:
                    self.two_started.set()
                try:
                    await self.release.wait()
                    await asyncio.to_thread(output_path.write_bytes, b"converted")
                finally:
                    self.active -= 1

        converter = GatedConverter()
        cache = ArtifactCache(cache_path, 60)
        await cache.startup()
        service = ConversionService(_Acquisition(), ConverterRegistry([converter]), cache)
        requests = [
            asyncio.create_task(service.convert(f"lifecycle-{index}", "epub")) for index in range(3)
        ]

        await asyncio.wait_for(converter.two_started.wait(), 2)
        await asyncio.sleep(0.05)
        assert converter.calls == 2
        converter.release.set()
        results = await asyncio.gather(*requests)

        assert converter.maximum_active == 2
        await asyncio.gather(*(result.stream.aclose() for result in results))
        await service.shutdown()

    asyncio.run(exercise(tmp_path / "first"))
    asyncio.run(exercise(tmp_path / "second"))


async def test_converter_directory_output_preserves_invalid_output_error(tmp_path: Path) -> None:
    cache = ArtifactCache(tmp_path, 60)
    await cache.startup()

    async def produce(output: Path) -> None:
        await asyncio.to_thread(output.mkdir)

    with pytest.raises(InvalidConversionOutputError, match="no valid output"):
        await cache.get_or_create(_key(), produce)
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.tmp")))
    await cache.shutdown()
