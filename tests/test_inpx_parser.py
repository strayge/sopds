"""Streaming and strictness tests for the independent INPX parser."""

import stat
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo
from zlib import error as ZlibError

import pytest

from sopds.imports.inpx import InpxParserError, parse_inpx
from sopds.imports.inpx import parser as parser_module

FIXTURES = Path(__file__).parent / "fixtures" / "inpx"
SEPARATOR = "\x04"
IMPLICIT_FIELDS = (
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


def test_implicit_layout_maps_unicode_metadata_and_physical_location() -> None:
    with parse_inpx(FIXTURES / "implicit.inpx") as records:
        first, second = tuple(records)

    assert first.locator.archive_relative_path == PurePosixPath("nested/catalog.zip")
    assert first.locator.member_filename == "book-001.fb2"
    assert first.authors == ("Иванов, Иван", "Smith, John")
    assert first.genres == ("sf", "prose")
    assert first.title == " Космос "
    assert first.series == "Цикл"
    assert first.series_number == "том 2"
    assert first.size == 12345
    assert first.library_id == "lib-77"
    assert first.deleted is False
    assert first.date == "2024-01-02"
    assert first.language == "ru"
    assert first.library_rating == 5
    assert first.keywords == "космос, space"
    assert first.extension_fields == ()
    assert second.deleted is True
    assert second.series is None
    assert second.keywords is None


def test_title_and_series_replace_em_dashes_with_en_dashes(tmp_path: Path) -> None:
    line = _implicit_line(
        AUTHOR="Writer\N{EM DASH}Name:",
        TITLE="Title\N{EM DASH}Subtitle",
        SERIES="Series\N{EM DASH}Part",
    )
    archive_path = _write_archive(tmp_path / "dashes.inpx", [("books.inp", line)])

    with parse_inpx(archive_path) as records:
        record = next(records)

    assert record.title == "Title\N{EN DASH}Subtitle"
    assert record.series == "Series\N{EN DASH}Part"
    assert record.authors == ("Writer\N{EM DASH}Name",)


def test_declared_layout_maps_by_normalized_name_and_preserves_unknown_fields() -> None:
    with parse_inpx(FIXTURES / "declared.inpx") as records:
        first, second = tuple(records)

    assert first.title == "Reordered"
    assert first.locator.member_filename == "english-book.mobi"
    assert first.series_number == "A-07"
    assert first.language is None
    assert first.keywords == "one, two"
    assert [(field.name, field.value) for field in first.extension_fields] == [
        ("X-CUSTOM", " opaque value ")
    ]
    assert second.title == "Совместимость"
    assert second.extension_fields[0].value == "добавка"


def test_invalid_utf8_has_entry_and_line_context_without_record_contents() -> None:
    with (
        parse_inpx(FIXTURES / "invalid-utf8.inpx") as records,
        pytest.raises(InpxParserError) as caught,
    ):
        next(records)

    assert caught.value.source_entry == "broken.inp"
    assert caught.value.line_number == 1
    assert "valid UTF-8" in str(caught.value)
    assert "Title" not in str(caught.value)


def test_iteration_is_lazy_and_an_error_closes_resources(tmp_path: Path) -> None:
    valid = _implicit_line(TITLE="first", FILE="one")
    invalid = _implicit_line(TITLE="must-not-leak", FILE="two", DEL="2")
    archive_path = _write_archive(tmp_path / "streaming.inpx", [("books.inp", valid + invalid)])

    records = parse_inpx(archive_path)
    assert next(records).title == "first"
    assert records.closed is False

    try:
        next(records)
    except InpxParserError as error:
        assert "DEL must" in str(error)
        assert "must-not-leak" not in str(error)
    else:
        pytest.fail("Malformed second record was accepted")

    assert records.closed is True


def test_context_manager_closes_after_partial_iteration(tmp_path: Path) -> None:
    payload = _implicit_line(TITLE="one", FILE="one") + _implicit_line(TITLE="two", FILE="two")
    archive_path = _write_archive(tmp_path / "partial.inpx", [("books.inp", payload)])

    with parse_inpx(archive_path) as records:
        assert next(records).title == "one"
        assert records.closed is False

    assert records.closed is True


def test_direct_close_stops_partial_iteration(tmp_path: Path) -> None:
    payload = _implicit_line(TITLE="one", FILE="one") + _implicit_line(TITLE="two", FILE="two")
    archive_path = _write_archive(tmp_path / "direct-close.inpx", [("books.inp", payload)])

    records = parse_inpx(archive_path)
    assert next(records).title == "one"

    records.close()

    assert records.closed is True
    with pytest.raises(StopIteration):
        next(records)


def test_corrupted_compressed_structure_is_translated_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = _write_corrupted_archive(
        tmp_path / "corrupt-structure.inpx",
        [
            ("structure.info", b"AUTHOR;GENRE;TITLE;FILE;SIZE;DEL;EXT"),
            ("books.inp", _implicit_line()),
        ],
        "structure.info",
    )
    opened_archives: list[ZipFile] = []

    def tracking_zip_file(path: Path) -> ZipFile:
        archive = ZipFile(path)
        opened_archives.append(archive)
        return archive

    monkeypatch.setattr(parser_module, "ZipFile", tracking_zip_file)

    with pytest.raises(InpxParserError, match=r"Could not read structure\.info") as caught:
        parse_inpx(archive_path)

    assert caught.value.source_entry == "structure.info"
    assert caught.value.line_number is None
    assert isinstance(caught.value.__cause__, ZlibError)
    assert opened_archives[0].fp is None


def test_corrupted_compressed_inp_is_translated_and_closes_iterator(tmp_path: Path) -> None:
    archive_path = _write_corrupted_archive(
        tmp_path / "corrupt-records.inpx",
        [("books.inp", _implicit_line())],
        "books.inp",
    )
    records = parse_inpx(archive_path)

    with pytest.raises(InpxParserError, match="Could not read INPX entry") as caught:
        next(records)

    assert caught.value.source_entry == "books.inp"
    assert caught.value.line_number is None
    assert isinstance(caught.value.__cause__, ZlibError)
    assert records.closed is True


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"SIZE": "-1"}, "SIZE must be a nonnegative integer"),
        ({"SIZE": " 1"}, "SIZE must be a nonnegative integer"),
        ({"DEL": "yes"}, "DEL must be empty, 0, or 1"),
        ({"LIBRATE": "0"}, "LIBRATE must be an integer from 1 to 5"),
        ({"LIBRATE": "6"}, "LIBRATE must be an integer from 1 to 5"),
        ({"FILE": "../book"}, "FILE is unsafe"),
        ({"EXT": "fb2\\bad"}, "EXT is unsafe"),
        ({"FILE": "bad\x00name"}, "FILE is unsafe"),
    ],
)
def test_invalid_field_values_fail_strictly(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    archive_path = _write_archive(
        tmp_path / "invalid.inpx", [("books.inp", _implicit_line(**overrides))]
    )

    with parse_inpx(archive_path) as records, pytest.raises(InpxParserError, match=message):
        next(records)


def test_implicit_layout_requires_final_empty_compatibility_slot(tmp_path: Path) -> None:
    values = _implicit_values()
    payload = SEPARATOR.join(values).encode() + b"\r\n"
    archive_path = _write_archive(tmp_path / "no-slot.inpx", [("books.inp", payload)])

    with (
        parse_inpx(archive_path) as records,
        pytest.raises(InpxParserError, match="compatibility slot"),
    ):
        next(records)


def test_records_require_crlf_line_endings(tmp_path: Path) -> None:
    payload = _implicit_line().removesuffix(b"\r\n") + b"\n"
    archive_path = _write_archive(tmp_path / "lf.inpx", [("books.inp", payload)])

    with parse_inpx(archive_path) as records, pytest.raises(InpxParserError, match="CRLF"):
        next(records)


@pytest.mark.parametrize(
    "structure",
    [
        "AUTHOR;AUTHOR;GENRE;TITLE;FILE;SIZE;DEL;EXT",
        "AUTHOR;GENRE;TITLE;FILE;SIZE;DEL",
        "AUTHOR;;GENRE;TITLE;FILE;SIZE;DEL;EXT",
        "AUTHOR;GÉNRE;TITLE;FILE;SIZE;DEL;EXT",
    ],
)
def test_invalid_structure_declarations_are_rejected(tmp_path: Path, structure: str) -> None:
    archive_path = _write_archive(
        tmp_path / "structure.inpx",
        [("structure.info", structure.encode()), ("books.inp", _implicit_line())],
    )

    with pytest.raises(InpxParserError):
        parse_inpx(archive_path)


def test_declared_layout_rejects_more_than_one_compatibility_value(tmp_path: Path) -> None:
    structure = "AUTHOR;GENRE;TITLE;FILE;SIZE;DEL;EXT"
    values = ["Author", "genre", "Title", "book", "1", "0", "fb2", "", ""]
    payload = SEPARATOR.join(values).encode() + b"\r\n"
    archive_path = _write_archive(
        tmp_path / "field-count.inpx",
        [("structure.info", structure.encode()), ("books.inp", payload)],
    )

    with (
        parse_inpx(archive_path) as records,
        pytest.raises(InpxParserError, match="field count"),
    ):
        next(records)


@pytest.mark.parametrize(
    "entry_name", ["../books.inp", "/books.inp", "C:/books.inp", "dir\\books.inp"]
)
def test_unsafe_inp_entry_paths_are_rejected_without_echoing_them(
    tmp_path: Path, entry_name: str
) -> None:
    archive_path = _write_archive(tmp_path / "unsafe.inpx", [(entry_name, _implicit_line())])

    with pytest.raises(InpxParserError) as caught:
        parse_inpx(archive_path)

    assert caught.value.source_entry is None
    assert entry_name not in str(caught.value)


def test_symlink_inp_entry_is_rejected(tmp_path: Path) -> None:
    archive_path = tmp_path / "symlink.inpx"
    entry = ZipInfo("books.inp")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive_path, "w") as archive:
        archive.writestr(entry, "target")

    with pytest.raises(InpxParserError, match="symlink") as caught:
        parse_inpx(archive_path)

    assert caught.value.source_entry == "books.inp"


def _implicit_values(**overrides: str) -> list[str]:
    values = {
        "AUTHOR": "Author, Unparsed:",
        "GENRE": "genre:",
        "TITLE": "Title",
        "SERIES": "",
        "SERNO": "",
        "FILE": "book",
        "SIZE": "1",
        "LIBID": "",
        "DEL": "0",
        "EXT": "fb2",
        "DATE": "",
        "LANG": "",
        "LIBRATE": "",
        "KEYWORDS": "",
    }
    values.update(overrides)
    return [values[name] for name in IMPLICIT_FIELDS]


def _implicit_line(**overrides: str) -> bytes:
    return (SEPARATOR.join(_implicit_values(**overrides)) + SEPARATOR + "\r\n").encode()


def _write_archive(path: Path, entries: Iterable[tuple[str, bytes]]) -> Path:
    with ZipFile(path, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return path


def _write_corrupted_archive(
    path: Path, entries: Iterable[tuple[str, bytes]], corrupted_entry: str
) -> Path:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
        entry = archive.getinfo(corrupted_entry)

    raw = bytearray(path.read_bytes())
    name_length = int.from_bytes(raw[entry.header_offset + 26 : entry.header_offset + 28], "little")
    extra_length = int.from_bytes(
        raw[entry.header_offset + 28 : entry.header_offset + 30], "little"
    )
    compressed_offset = entry.header_offset + 30 + name_length + extra_length
    raw[compressed_offset] = raw[compressed_offset] & 0xF8 | 0x07
    path.write_bytes(raw)
    return path
