"""Pinned local converter adapters and structural artifact validation."""

import asyncio
import os
import shutil
import stat
import struct
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from itertools import pairwise
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree

from sopds.conversion.contracts import (
    ConversionCapability,
    ConverterIdentity,
    InvalidConversionOutputError,
)
from sopds.conversion.process import ProcessResult, run_process

ProcessRunner = Callable[[tuple[str, ...]], Awaitable[ProcessResult]]
_EPUB_CAPABILITY = ConversionCapability("fb2", "epub", "application/epub+zip", "epub")
_FB2_AZW3_CAPABILITY = ConversionCapability("fb2", "azw3", "application/vnd.amazon.ebook", "azw3")
_EPUB_AZW3_CAPABILITY = ConversionCapability("epub", "azw3", "application/vnd.amazon.ebook", "azw3")


def _invalid_output() -> InvalidConversionOutputError:
    return InvalidConversionOutputError("Converter produced invalid output")


def _executable_file(path: str) -> bool:
    try:
        return stat.S_ISREG(Path(path).stat().st_mode) and os.access(path, os.X_OK)
    except OSError:
        return False


def _open_regular_artifact(path: Path) -> tuple[BinaryIO, int]:
    """Bind validation to one nonblocking descriptor without following substitutions."""
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise _invalid_output()
    flags = os.O_RDONLY | nofollow | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise _invalid_output() from error
    try:
        result = os.fstat(descriptor)
        if not stat.S_ISREG(result.st_mode) or result.st_size <= 0:
            raise _invalid_output()
        artifact = os.fdopen(descriptor, "rb")
        descriptor = -1
        return artifact, result.st_size
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)


def validate_epub(path: Path) -> None:
    """Reject EPUBs that cannot satisfy the required ZIP and package-root invariants."""
    try:
        artifact, _size = _open_regular_artifact(path)
        with artifact, zipfile.ZipFile(artifact) as archive:
            entries = archive.infolist()
            if not entries:
                raise _invalid_output()
            first = entries[0]
            if (
                first.filename != "mimetype"
                or first.header_offset != 0
                or first.compress_type != zipfile.ZIP_STORED
                or archive.read(first) != b"application/epub+zip"
            ):
                raise _invalid_output()
            if archive.testzip() is not None:
                raise _invalid_output()
            raw_container = archive.read("META-INF/container.xml")
            lowered_container = raw_container.lower()
            if b"<!doctype" in lowered_container or b"<!entity" in lowered_container:
                raise _invalid_output()
            container = ElementTree.fromstring(raw_container)  # noqa: S314
            rootfile = container.find("{*}rootfiles/{*}rootfile")
            package_path = rootfile.get("full-path") if rootfile is not None else None
            if not package_path or package_path not in archive.namelist():
                raise _invalid_output()
    except InvalidConversionOutputError:
        raise
    except Exception as error:
        raise _invalid_output() from error


def validate_azw3(path: Path) -> None:
    """Reject truncated or marker-free PalmDB/MOBI output before cache publication."""
    try:
        artifact, size = _open_regular_artifact(path)
        with artifact:
            header = artifact.read(78)
            if len(header) != 78 or header[60:68] != b"BOOKMOBI":
                raise _invalid_output()
            record_count = struct.unpack(">H", header[76:78])[0]
            if record_count <= 0:
                raise _invalid_output()
            table = artifact.read(record_count * 8)
            if len(table) != record_count * 8 or artifact.read(2) != b"\x00\x00":
                raise _invalid_output()
            offsets = [
                struct.unpack(">I", table[index : index + 4])[0]
                for index in range(0, len(table), 8)
            ]
            if (
                offsets[0] < 78 + len(table) + 2
                or any(left >= right for left, right in pairwise(offsets))
                or any(offset >= size for offset in offsets)
            ):
                raise _invalid_output()
            first_record_end = offsets[1] if len(offsets) > 1 else size
            if offsets[0] + 20 > first_record_end:
                raise _invalid_output()
            artifact.seek(offsets[0] + 16)
            if artifact.read(4) != b"MOBI":
                raise _invalid_output()
    except InvalidConversionOutputError:
        raise
    except Exception as error:
        raise _invalid_output() from error


async def _blocking[T](
    function: Callable[[], T], *, cancel_cleanup: Callable[[T], None] | None = None
) -> T:
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
        if not task.cancelled() and task.exception() is None and cancel_cleanup is not None:
            with suppress(BaseException):
                cancel_cleanup(task.result())
        raise


def _restore_owner_directory_access(root: Path, directory: Path) -> bool:
    """Recover traversal of an owned directory while refusing to chmod symlinks."""
    if directory != root and root not in directory.parents:
        return False
    try:
        result = directory.stat(follow_symlinks=False)
        if not stat.S_ISDIR(result.st_mode):
            return False
        mode = stat.S_IMODE(result.st_mode)
        required = stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        if mode & required == required:
            return False
        directory.chmod(mode | required)
    except OSError:
        return False
    return True


def _discard_converter_path(raw_path: str) -> None:
    """Remove adapter-owned output trees without following converter-created symlinks."""
    path = Path(raw_path)
    with suppress(OSError):
        result = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(result.st_mode):
            path.unlink()
            return

        recovered_permissions = True
        while recovered_permissions:
            recovered_permissions = False

            def recover_permission(
                function: Callable[..., object], failed_path: str, error: BaseException
            ) -> None:
                nonlocal recovered_permissions
                if not isinstance(error, PermissionError):
                    return
                directory = Path(failed_path)
                if function in (os.unlink, os.rmdir):
                    directory = directory.parent
                recovered_permissions |= _restore_owner_directory_access(path, directory)

            shutil.rmtree(path, onexc=recover_permission)


async def _remove(path: Path) -> None:
    await _blocking(lambda: _discard_converter_path(os.fspath(path)))


async def _validate(path: Path, validator: Callable[[Path], None]) -> None:
    try:
        await _blocking(lambda: validator(path))
    except BaseException:
        await _remove(path)
        raise


class Fb2ToEpubConverter:
    """Own the pinned fb2cng invocation and validate its EPUB2 artifact."""

    def __init__(self, executable: str = "fbc", runner: ProcessRunner = run_process) -> None:
        self._executable = executable
        self._runner = runner

    @property
    def identity(self) -> ConverterIdentity:
        return ConverterIdentity("fb2cng", "1.6.1")

    @property
    def capabilities(self) -> tuple[ConversionCapability, ...]:
        return (_EPUB_CAPABILITY,)

    def check_health(self) -> bool:
        return _executable_file(self._executable)

    async def convert(self, source_path: Path, target_format: str, output_path: Path) -> None:
        del target_format
        await _remove(output_path)
        try:
            await self._runner(
                (
                    self._executable,
                    "convert",
                    "--to",
                    "epub2",
                    "--output-file",
                    os.fspath(output_path),
                    os.fspath(source_path),
                )
            )
            await _validate(output_path, validate_epub)
        except BaseException:
            await _remove(output_path)
            raise


class EpubToAzw3Converter:
    """Retain Kindling preflight for direct EPUB input and validate its AZW3 artifact."""

    def __init__(
        self, executable: str = "kindling-cli", runner: ProcessRunner = run_process
    ) -> None:
        self._executable = executable
        self._runner = runner

    @property
    def identity(self) -> ConverterIdentity:
        return ConverterIdentity("kindling", "0.38.0")

    @property
    def capabilities(self) -> tuple[ConversionCapability, ...]:
        return (_EPUB_AZW3_CAPABILITY,)

    def check_health(self) -> bool:
        return _executable_file(self._executable)

    async def convert(self, source_path: Path, target_format: str, output_path: Path) -> None:
        del target_format
        await _remove(output_path)
        try:
            await self._runner(
                (
                    self._executable,
                    "build",
                    os.fspath(source_path),
                    "-o",
                    os.fspath(output_path),
                    "--no-embed-source",
                )
            )
            await _validate(output_path, validate_azw3)
        except BaseException:
            await _remove(output_path)
            raise


class Fb2ToAzw3Converter:
    """Hold one adapter call across the pinned EPUB2-to-AZW3 conversion pipeline."""

    def __init__(
        self,
        fbc_executable: str = "fbc",
        kindling_executable: str = "kindling-cli",
        runner: ProcessRunner = run_process,
    ) -> None:
        self._fbc_executable = fbc_executable
        self._kindling_executable = kindling_executable
        self._runner = runner

    @property
    def identity(self) -> ConverterIdentity:
        return ConverterIdentity("fb2cng-kindling", "1.6.1-0.38.0")

    @property
    def capabilities(self) -> tuple[ConversionCapability, ...]:
        return (_FB2_AZW3_CAPABILITY,)

    def check_health(self) -> bool:
        return _executable_file(self._fbc_executable) and _executable_file(
            self._kindling_executable
        )

    async def convert(self, source_path: Path, target_format: str, output_path: Path) -> None:
        del target_format
        await _remove(output_path)
        raw_intermediate_directory = await _blocking(
            lambda: tempfile.mkdtemp(prefix="sopds-conversion-", dir=output_path.parent),
            cancel_cleanup=_discard_converter_path,
        )
        intermediate_directory = Path(raw_intermediate_directory)
        intermediate = intermediate_directory / "intermediate.epub"
        try:
            await self._runner(
                (
                    self._fbc_executable,
                    "convert",
                    "--to",
                    "epub2",
                    "--output-file",
                    os.fspath(intermediate),
                    os.fspath(source_path),
                )
            )
            await _validate(intermediate, validate_epub)
            await self._runner(
                (
                    self._kindling_executable,
                    "build",
                    os.fspath(intermediate),
                    "-o",
                    os.fspath(output_path),
                    "--no-validate",
                    "--no-embed-source",
                )
            )
            await _validate(output_path, validate_azw3)
        except BaseException:
            await _remove(output_path)
            raise
        finally:
            await _remove(intermediate)
            await _remove(intermediate_directory)


__all__ = [
    "EpubToAzw3Converter",
    "Fb2ToAzw3Converter",
    "Fb2ToEpubConverter",
    "validate_azw3",
    "validate_epub",
]
