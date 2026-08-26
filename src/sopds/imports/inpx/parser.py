"""Strict, bounded-memory parsing of INPX catalog archives."""

from __future__ import annotations

import re
import stat
from collections.abc import Generator, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import TracebackType
from typing import override
from zipfile import BadZipFile, ZipFile, ZipInfo
from zlib import error as ZlibError

from sopds.imports.inpx.records import InpxExtensionField, InpxRecord, PhysicalBookLocator

_FIELD_SEPARATOR = "\x04"
_IMPLICIT_FIELDS = (
    "AUTHOR",
    "GENRE",
    "TITLE",
    "SERIES",
    "SERNO",
    "FILE",
    "SIZE",
    "LIBID",
    "DEL",
    "EXT",
    "DATE",
    "LANG",
    "LIBRATE",
    "KEYWORDS",
)
_PHYSICAL_IDENTITY_FIELDS = frozenset({"FILE", "EXT"})
_DESCRIPTION_FIELDS = frozenset({"AUTHOR", "GENRE", "TITLE"})
_RECORD_VALIDATION_FIELDS = frozenset({"SIZE", "DEL"})
_REQUIRED_FIELDS = _PHYSICAL_IDENTITY_FIELDS | _DESCRIPTION_FIELDS | _RECORD_VALIDATION_FIELDS
_KNOWN_FIELDS = frozenset(_IMPLICIT_FIELDS)
_INTEGER_PATTERN = re.compile(r"[0-9]+\Z")
_MAX_RECORD_BYTES = 4 * 1024 * 1024
_MAX_STRUCTURE_BYTES = 64 * 1024


class InpxParserError(ValueError):
    """Reports format context while deliberately excluding raw record contents."""

    def __init__(
        self,
        message: str,
        *,
        source_entry: str | None = None,
        line_number: int | None = None,
    ) -> None:
        self.source_entry = source_entry
        self.line_number = line_number
        context = ""
        if source_entry is not None:
            context = f" in {source_entry!r}"
        if line_number is not None:
            context += f" at line {line_number}"
        super().__init__(f"{message}{context}")


@dataclass(frozen=True, slots=True)
class _Schema:
    fields: tuple[str, ...]
    implicit_compatibility_slot: bool


class InpxRecordIterator(AbstractContextManager["InpxRecordIterator"], Iterator[InpxRecord]):
    """Owns the ZIP resources so callers can deterministically close partial iteration."""

    def __init__(self, path: Path) -> None:
        self._closed = False
        try:
            self._archive = ZipFile(path)
            schema = _read_schema(self._archive)
            entries = _record_entries(self._archive)
            self._records = self._iterate_records(schema, entries)
        except InpxParserError:
            self._close_archive_if_open()
            raise
        except (OSError, BadZipFile) as error:
            self._close_archive_if_open()
            raise InpxParserError("Could not open INPX archive") from error

    @property
    def closed(self) -> bool:
        return self._closed

    @override
    def __iter__(self) -> InpxRecordIterator:
        return self

    @override
    def __next__(self) -> InpxRecord:
        if self._closed:
            raise StopIteration
        try:
            return next(self._records)
        except StopIteration:
            self.close()
            raise
        except Exception:
            self.close()
            raise

    @override
    def __enter__(self) -> InpxRecordIterator:
        return self

    @override
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        records = getattr(self, "_records", None)
        if records is not None:
            records.close()
        self._close_archive_if_open()

    def _close_archive_if_open(self) -> None:
        archive = getattr(self, "_archive", None)
        if archive is not None:
            archive.close()

    def _iterate_records(
        self, schema: _Schema, entries: tuple[ZipInfo, ...]
    ) -> Generator[InpxRecord]:
        try:
            for entry in entries:
                source_entry = entry.filename
                archive_path = PurePosixPath(source_entry).with_suffix(".zip")
                try:
                    with self._archive.open(entry) as stream:
                        line_number = 0
                        while line := stream.readline(_MAX_RECORD_BYTES + 1):
                            line_number += 1
                            if len(line) > _MAX_RECORD_BYTES:
                                raise InpxParserError(
                                    "INPX record exceeds the size limit",
                                    source_entry=source_entry,
                                    line_number=line_number,
                                )
                            yield _parse_line(line, schema, archive_path, source_entry, line_number)
                except InpxParserError:
                    raise
                except (
                    OSError,
                    BadZipFile,
                    RuntimeError,
                    NotImplementedError,
                    EOFError,
                    ZlibError,
                ) as error:
                    raise InpxParserError(
                        "Could not read INPX entry", source_entry=source_entry
                    ) from error
        finally:
            self._archive.close()


def parse_inpx(path: Path) -> InpxRecordIterator:
    """Open an INPX archive as a synchronous, context-managed streaming iterator."""
    return InpxRecordIterator(path)


def _read_schema(archive: ZipFile) -> _Schema:
    structure_entries = [
        entry for entry in archive.infolist() if entry.filename == "structure.info"
    ]
    if not structure_entries:
        return _Schema(_IMPLICIT_FIELDS, implicit_compatibility_slot=True)
    if len(structure_entries) != 1:
        raise InpxParserError("INPX archive contains multiple structure.info entries")

    entry = structure_entries[0]
    if _is_symlink(entry):
        raise InpxParserError("structure.info must not be a symlink", source_entry="structure.info")
    try:
        with archive.open(entry) as stream:
            raw = stream.read(_MAX_STRUCTURE_BYTES + 1)
    except (
        OSError,
        BadZipFile,
        RuntimeError,
        NotImplementedError,
        EOFError,
        ZlibError,
    ) as error:
        raise InpxParserError(
            "Could not read structure.info", source_entry="structure.info"
        ) from error
    if len(raw) > _MAX_STRUCTURE_BYTES:
        raise InpxParserError(
            "structure.info exceeds the size limit", source_entry="structure.info"
        )
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise InpxParserError(
            "structure.info is not valid UTF-8", source_entry="structure.info"
        ) from error

    text = _remove_optional_line_ending(text)
    if "\n" in text or "\r" in text:
        raise InpxParserError(
            "structure.info contains multiple lines", source_entry="structure.info"
        )
    if text.endswith(";"):
        text = text[:-1]
    raw_names = text.split(";")
    if not raw_names or any(not name for name in raw_names):
        raise InpxParserError(
            "structure.info contains an empty field name", source_entry="structure.info"
        )

    names = tuple(_normalize_field_name(name) for name in raw_names)
    if len(set(names)) != len(names):
        raise InpxParserError(
            "structure.info contains duplicate field names", source_entry="structure.info"
        )
    missing = sorted(_REQUIRED_FIELDS.difference(names))
    if missing:
        raise InpxParserError(
            f"structure.info is missing required fields: {', '.join(missing)}",
            source_entry="structure.info",
        )
    return _Schema(names, implicit_compatibility_slot=False)


def _normalize_field_name(name: str) -> str:
    if not name.isascii() or any(
        ord(character) < 33 or ord(character) == 127 for character in name
    ):
        raise InpxParserError(
            "structure.info contains an invalid field name", source_entry="structure.info"
        )
    return name.upper()


def _record_entries(archive: ZipFile) -> tuple[ZipInfo, ...]:
    entries: list[ZipInfo] = []
    for entry in archive.infolist():
        if not entry.filename.lower().endswith(".inp"):
            continue
        safe_name = _safe_entry_name(entry.filename)
        if _is_symlink(entry):
            raise InpxParserError("INPX data entry must not be a symlink", source_entry=safe_name)
        entries.append(entry)
    if not entries:
        raise InpxParserError("INPX archive contains no .inp entries")
    return tuple(entries)


def _safe_entry_name(name: str) -> str:
    path = PurePosixPath(name)
    unsafe = (
        not name
        or "\\" in name
        or _has_control_character(name)
        or path.is_absolute()
        or PureWindowsPath(name).is_absolute()
        or any(part in {"", ".", ".."} for part in name.split("/"))
    )
    if unsafe:
        raise InpxParserError("INPX archive contains an unsafe .inp entry path")
    return name


def _is_symlink(entry: ZipInfo) -> bool:
    mode = entry.external_attr >> 16
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _parse_line(
    raw_line: bytes,
    schema: _Schema,
    archive_path: PurePosixPath,
    source_entry: str,
    line_number: int,
) -> InpxRecord:
    if not raw_line.endswith(b"\r\n"):
        raise InpxParserError(
            "INPX record does not end with CRLF",
            source_entry=source_entry,
            line_number=line_number,
        )
    try:
        text = raw_line[:-2].decode("utf-8")
    except UnicodeDecodeError as error:
        raise InpxParserError(
            "INPX record is not valid UTF-8",
            source_entry=source_entry,
            line_number=line_number,
        ) from error

    values = text.split(_FIELD_SEPARATOR)
    if schema.implicit_compatibility_slot:
        if len(values) != len(schema.fields) + 1 or values[-1] != "":
            raise InpxParserError(
                "Implicit INPX record has an invalid field count or compatibility slot",
                source_entry=source_entry,
                line_number=line_number,
            )
        values = values[:-1]
    elif len(values) == len(schema.fields) + 1 and values[-1] == "":
        values = values[:-1]
    elif len(values) != len(schema.fields):
        raise InpxParserError(
            "Declared INPX record has an invalid field count",
            source_entry=source_entry,
            line_number=line_number,
        )

    fields = dict(zip(schema.fields, values, strict=True))
    return _make_record(fields, archive_path, source_entry, line_number)


def _make_record(
    fields: dict[str, str],
    archive_path: PurePosixPath,
    source_entry: str,
    line_number: int,
) -> InpxRecord:
    filename = fields["FILE"]
    extension = fields["EXT"]
    _validate_member_component(filename, "FILE", source_entry, line_number)
    _validate_member_component(extension, "EXT", source_entry, line_number)
    size = _parse_nonnegative_integer(fields["SIZE"], "SIZE", source_entry, line_number)

    deleted_value = fields["DEL"]
    if deleted_value not in {"", "0", "1"}:
        raise InpxParserError(
            "DEL must be empty, 0, or 1", source_entry=source_entry, line_number=line_number
        )

    rating_text = fields.get("LIBRATE", "")
    rating: int | None = None
    if rating_text:
        rating = _parse_nonnegative_integer(rating_text, "LIBRATE", source_entry, line_number)
        if rating not in range(1, 6):
            raise InpxParserError(
                "LIBRATE must be an integer from 1 to 5",
                source_entry=source_entry,
                line_number=line_number,
            )

    return InpxRecord(
        locator=PhysicalBookLocator(archive_path, f"{filename}.{extension}"),
        authors=tuple(value for value in fields["AUTHOR"].split(":") if value),
        genres=tuple(value for value in fields["GENRE"].split(":") if value),
        title=fields["TITLE"],
        series=_optional(fields.get("SERIES", "")),
        series_number=_optional(fields.get("SERNO", "")),
        size=size,
        library_id=_optional(fields.get("LIBID", "")),
        deleted=deleted_value == "1",
        extension=extension,
        date=_optional(fields.get("DATE", "")),
        language=_optional(fields.get("LANG", "")),
        library_rating=rating,
        keywords=_optional(fields.get("KEYWORDS", "")),
        extension_fields=tuple(
            InpxExtensionField(name, value)
            for name, value in fields.items()
            if name not in _KNOWN_FIELDS
        ),
    )


def _validate_member_component(value: str, field: str, source_entry: str, line_number: int) -> None:
    if not value or "/" in value or "\\" in value or _has_control_character(value):
        raise InpxParserError(
            f"{field} is unsafe for a ZIP member filename",
            source_entry=source_entry,
            line_number=line_number,
        )


def _parse_nonnegative_integer(value: str, field: str, source_entry: str, line_number: int) -> int:
    if _INTEGER_PATTERN.fullmatch(value) is None:
        raise InpxParserError(
            f"{field} must be a nonnegative integer",
            source_entry=source_entry,
            line_number=line_number,
        )
    try:
        return int(value)
    except ValueError as error:
        raise InpxParserError(
            f"{field} integer is too large",
            source_entry=source_entry,
            line_number=line_number,
        ) from error


def _has_control_character(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _optional(value: str) -> str | None:
    return value if value else None


def _remove_optional_line_ending(value: str) -> str:
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith("\n"):
        return value[:-1]
    return value
