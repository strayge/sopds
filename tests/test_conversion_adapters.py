"""Concrete conversion adapter command, identity, validation, and cleanup tests."""

import asyncio
import os
import struct
import tempfile
import threading
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from sopds.conversion import adapters
from sopds.conversion.adapters import (
    EpubToAzw3Converter,
    Fb2ToAzw3Converter,
    Fb2ToEpubConverter,
    validate_azw3,
    validate_epub,
)
from sopds.conversion.contracts import (
    Converter,
    ConverterExecutionError,
    InvalidConversionOutputError,
)
from sopds.conversion.process import ProcessResult


def _write_epub(path: Path) -> None:
    container = (
        b'<?xml version="1.0"?>'
        b'<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
        b'<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OEBPS/content.opf", b"<package/>")


def _write_azw3(path: Path) -> None:
    header = bytearray(78)
    header[60:68] = b"BOOKMOBI"
    header[76:78] = struct.pack(">H", 1)
    record_offset = 88
    body = bytearray(record_offset + 32)
    body[:78] = header
    body[78:86] = struct.pack(">I", record_offset) + b"\x00\x00\x00\x01"
    body[record_offset + 16 : record_offset + 20] = b"MOBI"
    path.write_bytes(body)


class _RecordingRunner:
    def __init__(self, writer: Callable[[tuple[str, ...]], None]) -> None:
        self.writer = writer
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, argv: tuple[str, ...]) -> ProcessResult:
        self.calls.append(argv)
        self.writer(argv)
        return ProcessResult(b"", b"")


def _output_after(argv: tuple[str, ...], flag: str) -> Path:
    return Path(argv[argv.index(flag) + 1])


def test_adapter_health_requires_executable_regular_files(tmp_path: Path) -> None:
    fbc = tmp_path / "fbc"
    kindling = tmp_path / "kindling-cli"
    fbc.write_bytes(b"converter")
    kindling.write_bytes(b"converter")
    fbc.chmod(0o755)
    kindling.chmod(0o755)

    assert Fb2ToEpubConverter(str(fbc)).check_health()
    assert EpubToAzw3Converter(str(kindling)).check_health()
    assert Fb2ToAzw3Converter(str(fbc), str(kindling)).check_health()

    kindling.chmod(0o644)

    assert Fb2ToEpubConverter(str(fbc)).check_health()
    assert not EpubToAzw3Converter(str(kindling)).check_health()
    assert not Fb2ToAzw3Converter(str(fbc), str(kindling)).check_health()
    assert not Fb2ToEpubConverter(str(tmp_path)).check_health()
    assert not Fb2ToEpubConverter(str(tmp_path / "missing")).check_health()


async def test_direct_adapters_use_exact_argv_and_pinned_identities(tmp_path: Path) -> None:
    source = tmp_path / "source input.epub"
    source.write_bytes(b"source")

    epub_runner = _RecordingRunner(lambda argv: _write_epub(_output_after(argv, "--output-file")))
    epub = Fb2ToEpubConverter("/opt/fbc", epub_runner)
    epub_output = tmp_path / "output.epub"
    await epub.convert(source, "epub", epub_output)

    azw_runner = _RecordingRunner(lambda argv: _write_azw3(_output_after(argv, "-o")))
    azw = EpubToAzw3Converter("/opt/kindling", azw_runner)
    azw_output = tmp_path / "output.azw3"
    await azw.convert(source, "azw3", azw_output)

    assert epub_runner.calls == [
        (
            "/opt/fbc",
            "convert",
            "--to",
            "epub2",
            "--output-file",
            str(epub_output),
            str(source),
        )
    ]
    assert azw_runner.calls == [
        (
            "/opt/kindling",
            "build",
            str(source),
            "-o",
            str(azw_output),
            "--no-embed-source",
        )
    ]
    assert (epub.identity.name, epub.identity.version) == ("fb2cng", "1.6.1")
    assert (azw.identity.name, azw.identity.version) == ("kindling", "0.38.0")


async def test_fb2_pipeline_uses_nonexistent_private_epub_and_cleans_it(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(b"source")

    def write_output(argv: tuple[str, ...]) -> None:
        if argv[0] == "/opt/fbc":
            intermediate_output = _output_after(argv, "--output-file")
            assert not intermediate_output.exists()
            _write_epub(intermediate_output)
        else:
            _write_azw3(_output_after(argv, "-o"))

    runner = _RecordingRunner(write_output)
    converter = Fb2ToAzw3Converter("/opt/fbc", "/opt/kindling", runner)
    output = tmp_path / "book.azw3"

    await converter.convert(source, "azw3", output)

    intermediate = Path(runner.calls[0][runner.calls[0].index("--output-file") + 1])
    assert runner.calls == [
        (
            "/opt/fbc",
            "convert",
            "--to",
            "epub2",
            "--output-file",
            str(intermediate),
            str(source),
        ),
        (
            "/opt/kindling",
            "build",
            str(intermediate),
            "-o",
            str(output),
            "--no-validate",
            "--no-embed-source",
        ),
    ]
    assert not await asyncio.to_thread(intermediate.exists)
    assert not await asyncio.to_thread(intermediate.parent.exists)
    assert await asyncio.to_thread(output.exists)
    assert (converter.identity.name, converter.identity.version) == (
        "fb2cng-kindling",
        "1.6.1-0.38.0",
    )


@pytest.mark.parametrize("validator", [validate_epub, validate_azw3])
def test_structural_validators_reject_truncated_output(
    tmp_path: Path, validator: Callable[[Path], None]
) -> None:
    artifact = tmp_path / "truncated"
    artifact.write_bytes(b"BOOKMOBI")

    with pytest.raises(InvalidConversionOutputError):
        validator(artifact)


@pytest.mark.parametrize("validator", [validate_epub, validate_azw3])
@pytest.mark.parametrize("replacement", ["fifo", "symlink"])
def test_structural_validators_reject_special_files_promptly(
    tmp_path: Path, validator: Callable[[Path], None], replacement: str
) -> None:
    artifact = tmp_path / "artifact"
    if replacement == "fifo":
        os.mkfifo(artifact)
    else:
        target = tmp_path / "target"
        target.write_bytes(b"not an artifact")
        artifact.symlink_to(target)

    with pytest.raises(InvalidConversionOutputError):
        validator(artifact)


@pytest.mark.parametrize("validator", [validate_epub, validate_azw3])
def test_structural_validators_fail_safe_without_nofollow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    validator: Callable[[Path], None],
) -> None:
    artifact = tmp_path / "valid-artifact"
    if validator is validate_epub:
        _write_epub(artifact)
    else:
        _write_azw3(artifact)
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(InvalidConversionOutputError):
        validator(artifact)


def test_epub_validator_rejects_prefixed_archive(tmp_path: Path) -> None:
    artifact = tmp_path / "prefixed.epub"
    _write_epub(artifact)
    artifact.write_bytes(b"untrusted-prefix" + artifact.read_bytes())

    with pytest.raises(InvalidConversionOutputError):
        validate_epub(artifact)


def test_azw3_validator_rejects_mobi_marker_outside_first_record(tmp_path: Path) -> None:
    artifact = tmp_path / "marker-in-record-one.azw3"
    header = bytearray(78)
    header[60:68] = b"BOOKMOBI"
    header[76:78] = struct.pack(">H", 2)
    first_offset = 96
    second_offset = first_offset + 16
    body = bytearray(second_offset + 32)
    body[:78] = header
    body[78:86] = struct.pack(">I", first_offset) + b"\x00\x00\x00\x01"
    body[86:94] = struct.pack(">I", second_offset) + b"\x00\x00\x00\x02"
    body[94:96] = b"\x00\x00"
    body[second_offset : second_offset + 4] = b"MOBI"
    artifact.write_bytes(body)

    with pytest.raises(InvalidConversionOutputError):
        validate_azw3(artifact)


def test_azw3_validator_rejects_record_start_before_required_gap(tmp_path: Path) -> None:
    artifact = tmp_path / "missing-record-list-gap.azw3"
    header = bytearray(78)
    header[60:68] = b"BOOKMOBI"
    header[76:78] = struct.pack(">H", 1)
    record_offset = 86
    body = bytearray(record_offset + 32)
    body[:78] = header
    body[78:86] = struct.pack(">I", record_offset) + b"\x00\x00\x00\x01"
    body[record_offset + 16 : record_offset + 20] = b"MOBI"
    artifact.write_bytes(body)

    with pytest.raises(InvalidConversionOutputError):
        validate_azw3(artifact)


async def test_pipeline_removes_complete_private_directory_with_sidecars(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(b"source")
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_bytes(b"keep")

    def write_output(argv: tuple[str, ...]) -> None:
        if argv[0] == "fbc":
            intermediate = _output_after(argv, "--output-file")
            _write_epub(intermediate)
            (intermediate.parent / "converter-sidecar").write_bytes(b"sidecar")
            (intermediate.parent / "external-link").symlink_to(external, target_is_directory=True)
        else:
            _write_azw3(_output_after(argv, "-o"))

    runner = _RecordingRunner(write_output)
    output = tmp_path / "output.azw3"
    await Fb2ToAzw3Converter(runner=runner).convert(source, "azw3", output)

    intermediate = _output_after(runner.calls[0], "--output-file")
    assert not await asyncio.to_thread(intermediate.parent.exists)
    assert (external / "keep").read_bytes() == b"keep"


@pytest.mark.parametrize(
    "converter_factory",
    [
        lambda runner: Fb2ToEpubConverter(runner=runner),
        lambda runner: EpubToAzw3Converter(runner=runner),
        lambda runner: Fb2ToAzw3Converter(runner=runner),
    ],
)
async def test_adapters_remove_nonempty_output_directories_without_following_symlinks(
    tmp_path: Path,
    converter_factory: Callable[[adapters.ProcessRunner], Converter],
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"source")
    output = tmp_path / "output"
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_bytes(b"keep")

    async def create_output_directory(argv: tuple[str, ...]) -> ProcessResult:
        if len(argv) > 1 and argv[0] == "fbc" and isinstance(converter, Fb2ToAzw3Converter):
            _write_epub(_output_after(argv, "--output-file"))
        else:
            output.mkdir()
            (output / "converter-sidecar").write_bytes(b"partial")
            (output / "external-link").symlink_to(external, target_is_directory=True)
        return ProcessResult(b"", b"")

    converter = converter_factory(create_output_directory)

    with pytest.raises(InvalidConversionOutputError):
        await converter.convert(source, converter.capabilities[0].target_format, output)

    assert not await asyncio.to_thread(output.exists)
    assert (external / "keep").read_bytes() == b"keep"


async def test_direct_adapter_recovers_restrictive_nested_output_permissions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"source")
    output = tmp_path / "output.azw3"
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_bytes(b"keep")

    async def create_restrictive_output(argv: tuple[str, ...]) -> ProcessResult:
        converter_output = _output_after(argv, "-o")
        restricted = converter_output / "restricted"
        nested = restricted / "nested"
        nested.mkdir(parents=True)
        (nested / "partial").write_bytes(b"partial")
        (nested / "external-link").symlink_to(external, target_is_directory=True)
        nested.chmod(0)
        restricted.chmod(0)
        return ProcessResult(b"", b"")

    with pytest.raises(InvalidConversionOutputError):
        await EpubToAzw3Converter(runner=create_restrictive_output).convert(source, "azw3", output)

    assert not await asyncio.to_thread(output.exists)
    assert (external / "keep").read_bytes() == b"keep"


async def test_adapter_unlinks_output_symlink_without_following_target(tmp_path: Path) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"source")
    output = tmp_path / "output.azw3"
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_bytes(b"keep")

    async def create_output_symlink(argv: tuple[str, ...]) -> ProcessResult:
        _output_after(argv, "-o").symlink_to(external, target_is_directory=True)
        return ProcessResult(b"", b"")

    with pytest.raises(InvalidConversionOutputError):
        await EpubToAzw3Converter(runner=create_output_symlink).convert(source, "azw3", output)

    assert not output.is_symlink()
    assert (external / "keep").read_bytes() == b"keep"


async def test_pipeline_recovers_restrictive_private_tree_permissions(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(b"source")
    output = tmp_path / "output.azw3"
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_bytes(b"keep")
    private_directory: Path | None = None

    def write_output(argv: tuple[str, ...]) -> None:
        nonlocal private_directory
        if argv[0] == "fbc":
            intermediate = _output_after(argv, "--output-file")
            private_directory = intermediate.parent
            _write_epub(intermediate)
            restricted = private_directory / "restricted"
            nested = restricted / "nested"
            nested.mkdir(parents=True)
            (nested / "sidecar").write_bytes(b"partial")
            (nested / "external-link").symlink_to(external, target_is_directory=True)
            nested.chmod(0)
            restricted.chmod(0)
        else:
            _write_azw3(_output_after(argv, "-o"))

    await Fb2ToAzw3Converter(runner=_RecordingRunner(write_output)).convert(source, "azw3", output)

    assert private_directory is not None
    assert not await asyncio.to_thread(private_directory.exists)
    assert (external / "keep").read_bytes() == b"keep"


async def test_pipeline_removes_private_directory_for_malformed_directory_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(b"source")
    seen_directory: Path | None = None

    async def write_directory(argv: tuple[str, ...]) -> ProcessResult:
        nonlocal seen_directory
        seen_directory = _output_after(argv, "--output-file")
        seen_directory.mkdir()
        (seen_directory / "partial").write_bytes(b"partial")
        (seen_directory.parent / "sidecar").write_bytes(b"sidecar")
        return ProcessResult(b"", b"")

    with pytest.raises(InvalidConversionOutputError):
        await Fb2ToAzw3Converter(runner=write_directory).convert(
            source, "azw3", tmp_path / "output.azw3"
        )

    assert seen_directory is not None
    assert not await asyncio.to_thread(seen_directory.parent.exists)


async def test_pipeline_creation_cancellation_removes_complete_late_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(b"source")
    entered = threading.Event()
    release = threading.Event()
    created: Path | None = None
    real_mkdtemp = tempfile.mkdtemp

    def gated_mkdtemp(*, prefix: str, dir: Path) -> str:  # noqa: A002
        nonlocal created
        entered.set()
        assert release.wait(5)
        raw_path = real_mkdtemp(prefix=prefix, dir=dir)
        created = Path(raw_path)
        (created / "sidecar").write_bytes(b"partial")
        return raw_path

    async def must_not_run(_argv: tuple[str, ...]) -> ProcessResult:
        raise AssertionError("converter ran after cancellation")

    monkeypatch.setattr(tempfile, "mkdtemp", gated_mkdtemp)
    task = asyncio.create_task(
        Fb2ToAzw3Converter(runner=must_not_run).convert(source, "azw3", tmp_path / "output.azw3")
    )
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert created is not None
    assert not await asyncio.to_thread(created.exists)


async def test_pipeline_removes_intermediate_and_partial_output_on_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.fb2"
    source.write_bytes(b"source")
    seen_intermediate: Path | None = None

    async def fail_kindling(argv: tuple[str, ...]) -> ProcessResult:
        nonlocal seen_intermediate
        if argv[0] == "fbc":
            seen_intermediate = _output_after(argv, "--output-file")
            _write_epub(seen_intermediate)
        else:
            _output_after(argv, "-o").write_bytes(b"partial")
            raise ConverterExecutionError("Converter execution failed")
        return ProcessResult(b"", b"")

    output = tmp_path / "output.azw3"
    converter = Fb2ToAzw3Converter(runner=fail_kindling)

    with pytest.raises(ConverterExecutionError):
        await converter.convert(source, "azw3", output)

    assert seen_intermediate is not None
    assert not await asyncio.to_thread(seen_intermediate.exists)
    assert not await asyncio.to_thread(output.exists)


async def test_adapter_cancellation_preserves_cancellation_when_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"source")
    entered = threading.Event()
    release = threading.Event()

    def fail_after_cancellation(_path: Path) -> None:
        entered.set()
        assert release.wait(5)
        raise RuntimeError("private validation failure")

    runner = _RecordingRunner(lambda argv: _write_azw3(_output_after(argv, "-o")))
    monkeypatch.setattr(adapters, "validate_azw3", fail_after_cancellation)
    output = tmp_path / "output.azw3"
    task = asyncio.create_task(EpubToAzw3Converter(runner=runner).convert(source, "azw3", output))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await asyncio.to_thread(output.exists)


async def test_adapter_cancellation_removes_partial_output(tmp_path: Path) -> None:
    source = tmp_path / "source.epub"
    source.write_bytes(b"source")
    external = tmp_path / "external"
    external.mkdir()
    (external / "keep").write_bytes(b"keep")
    started = asyncio.Event()

    async def wait_forever(argv: tuple[str, ...]) -> ProcessResult:
        output = _output_after(argv, "-o")
        output.mkdir()
        (output / "converter-sidecar").write_bytes(b"partial")
        (output / "external-link").symlink_to(external, target_is_directory=True)
        started.set()
        await asyncio.Event().wait()
        raise AssertionError

    output = tmp_path / "output.azw3"
    task = asyncio.create_task(
        EpubToAzw3Converter(runner=wait_forever).convert(source, "azw3", output)
    )
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await asyncio.to_thread(output.exists)
    assert (external / "keep").read_bytes() == b"keep"
