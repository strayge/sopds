"""Selected-book request, manifest, and portable archive-path tests."""

import asyncio
import io
import tempfile
import threading
import unicodedata
import zipfile
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from typing import Literal, cast, override

import pytest

import sopds.acquisition.archive as archive_module
from sopds.acquisition.archive import (
    ARCHIVE_CHUNK_SIZE,
    MAX_COMPONENT_BYTES,
    MAX_ELIGIBLE_SIZE,
    MAX_PATH_BYTES,
    MAX_SELECTED_BOOKS,
    ArchiveEntryStatus,
    ArchiveInputError,
    ArchiveLimitError,
    ArchiveManifest,
    ArchiveNoDownloadsError,
    ArchivePreset,
    ArchiveRequest,
    ArchiveService,
    StagedArchive,
    archive_base_path,
    build_manifest,
    normalize_extension,
    portable_path_key,
    sanitize_component,
)
from sopds.acquisition.contracts import (
    AcquiredOriginal,
    AcquisitionAmbiguousMemberError,
    AcquisitionCorruptError,
    AcquisitionDirectoryMemberError,
    AcquisitionEncryptedMemberError,
    AcquisitionError,
    AcquisitionMemberNotFoundError,
    AcquisitionNotFoundError,
    AcquisitionSizeMismatchError,
    AcquisitionSourceIOError,
    AcquisitionStoreShutdownError,
    AcquisitionSymlinkMemberError,
    AcquisitionUnavailableError,
    AcquisitionUnsafePathError,
    OriginalDescription,
    SourceRevision,
)
from sopds.catalog.contracts import (
    BookAvailability,
    BookSummary,
    CatalogSummaryBatch,
)
from sopds.conversion.contracts import (
    ConversionResult,
    ConverterExecutionError,
    SourceUnavailableError,
)
from sopds.conversion.policy import OutputDecision


def _book(
    public_id: str,
    *,
    title: str = "Title",
    authors: tuple[str, ...] = ("Last, First",),
    series: str | None = "Series",
    series_number: str | None = "1",
    original_format: str = "FB2",
    size: int = 1,
    availability: BookAvailability = BookAvailability.ACTIVE,
    downloadable: bool = True,
) -> BookSummary:
    return BookSummary(
        public_id=public_id,
        title=title,
        authors=authors,
        series=series,
        series_number=series_number,
        language=None,
        original_format=original_format,
        size=size,
        availability=availability,
        downloadable=downloadable,
    )


def _manifest(
    ids: list[str],
    books: Sequence[BookSummary],
    preset: ArchivePreset | str = ArchivePreset.NESTED,
    *,
    generation_id: int | None = 7,
) -> ArchiveManifest:
    return build_manifest(
        ArchiveRequest(ids, preset),
        CatalogSummaryBatch(generation_id, tuple(books)),
    )


_REVISION = SourceRevision(1, 2, 3)


class _Stream:
    def __init__(
        self,
        chunks: Sequence[bytes],
        *,
        error: BaseException | None = None,
        on_open: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
        read_entered: asyncio.Event | None = None,
        read_release: asyncio.Event | None = None,
    ) -> None:
        self._chunks = iter(chunks)
        self._error = error
        self._on_open = on_open
        self._on_close = on_close
        self._read_entered = read_entered
        self._read_release = read_release
        self.closed = False

    def open(self) -> None:
        if self._on_open is not None:
            self._on_open()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if self._read_release is not None:
            if self._read_entered is not None:
                self._read_entered.set()
            release = self._read_release
            self._read_release = None
            await release.wait()
        try:
            return next(self._chunks)
        except StopIteration:
            if self._error is not None:
                raise self._error from None
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._on_close is not None:
            self._on_close()


class _Conversion:
    def __init__(
        self,
        values: Mapping[str, bytes | BaseException],
        supported: set[tuple[str, str]],
    ) -> None:
        self._values = values
        self._supported = supported
        self.calls: list[tuple[str, str, int | None]] = []
        self.streams: list[_Stream] = []

    def supports(self, source_format: str, target_format: str) -> bool:
        return (source_format.casefold(), target_format) in self._supported

    async def convert(
        self,
        public_id: str,
        target_format: str,
        *,
        expected_generation_id: int | None = None,
    ) -> ConversionResult:
        self.calls.append((public_id, target_format, expected_generation_id))
        value = self._values[public_id]
        if isinstance(value, BaseException):
            raise value
        stream = _Stream((value,))
        self.streams.append(stream)
        return ConversionResult(
            f"{public_id}.{target_format}",
            "application/epub+zip",
            len(value),
            stream,
        )


class _Catalog:
    def __init__(self, batches: Sequence[CatalogSummaryBatch]) -> None:
        self._batches = iter(batches)
        self.calls: list[tuple[str, ...]] = []

    async def bulk_summaries(self, public_ids: Sequence[str]) -> CatalogSummaryBatch:
        self.calls.append(tuple(public_ids))
        return next(self._batches)


class _Acquisition:
    def __init__(
        self,
        values: Mapping[str, bytes | BaseException | _Stream],
        sizes: dict[str, int] | None = None,
        *,
        acquire_entered: asyncio.Event | None = None,
        acquire_release: asyncio.Event | None = None,
    ) -> None:
        self._values = values
        self._sizes = sizes or {}
        self.calls: list[tuple[str, int | None]] = []
        self.streams: list[_Stream] = []
        self._acquire_entered = acquire_entered
        self._acquire_release = acquire_release

    async def describe(
        self,
        public_id: str,
        *,
        expected_generation_id: int | None = None,
    ) -> OriginalDescription:
        raise AssertionError("Archive builds do not describe originals")

    async def acquire(
        self,
        public_id: str,
        *,
        expected_generation_id: int | None = None,
    ) -> AcquiredOriginal:
        self.calls.append((public_id, expected_generation_id))
        if self._acquire_release is not None:
            if self._acquire_entered is not None:
                self._acquire_entered.set()
            await self._acquire_release.wait()
        value = self._values[public_id]
        if isinstance(value, BaseException):
            raise value
        stream = value if isinstance(value, _Stream) else _Stream((value,))
        stream.open()
        self.streams.append(stream)
        size = self._sizes[public_id] if isinstance(value, _Stream) else len(value)
        return AcquiredOriginal(
            f"{public_id}.fb2",
            "application/octet-stream",
            size,
            stream,
            "fb2",
            _REVISION,
        )


async def _archive_bytes(staged: StagedArchive) -> bytes:
    return b"".join([chunk async for chunk in staged])


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        (ArchivePreset.NESTED, "Last First/Series/01 - Title.fb2"),
        (ArchivePreset.FLATTEN, "Last First/Series 01 - Title.fb2"),
        (ArchivePreset.LIST, "Last First. Series 01 - Title.fb2"),
    ],
)
def test_all_presets_use_confirmed_series_layouts(preset: ArchivePreset, expected: str) -> None:
    assert archive_base_path(_book("book"), preset) == expected


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        (ArchivePreset.NESTED, "Unknown author/Title.fb2"),
        (ArchivePreset.FLATTEN, "Unknown author/Title.fb2"),
        (ArchivePreset.LIST, "Unknown author. Title.fb2"),
    ],
)
def test_missing_author_and_series_use_role_fallback_and_no_series_layout(
    preset: ArchivePreset, expected: str
) -> None:
    book = _book("book", authors=(), series=None, series_number=None)
    assert archive_base_path(book, preset) == expected


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        (ArchivePreset.NESTED, "Last First/Series/Title.fb2"),
        (ArchivePreset.FLATTEN, "Last First/Series - Title.fb2"),
        (ArchivePreset.LIST, "Last First. Series - Title.fb2"),
    ],
)
def test_missing_series_number_omits_adjacent_spacing(preset: ArchivePreset, expected: str) -> None:
    assert archive_base_path(_book("book", series_number=None), preset) == expected
    assert archive_base_path(_book("book", series_number=""), preset) == expected


def test_empty_series_and_title_use_component_role_fallbacks() -> None:
    book = _book("book", title=" . ", series="", series_number=None)
    assert archive_base_path(book, ArchivePreset.NESTED) == "Last First/Series/book.fb2"


@pytest.mark.parametrize(
    ("number", "expected"),
    [("1", "01"), ("12", "12"), ("123", "123"), ("Part-A", "Part-A")],
)
def test_numeric_series_numbers_have_minimum_width_two(number: str, expected: str) -> None:
    path = archive_base_path(_book("book", series_number=number), ArchivePreset.FLATTEN)
    assert path == f"Last First/Series {expected} - Title.fb2"


def test_request_validation_deduplicates_in_first_seen_order() -> None:
    request = ArchiveRequest(["b", "a", "b", "c", "a"], "flatten")

    assert request.ids == ("b", "a", "c")
    assert request.preset is ArchivePreset.FLATTEN


@pytest.mark.parametrize(
    ("ids", "preset"),
    [
        ([], "unknown"),
        ("book", "nested"),
        ([1], "nested"),
        ([""], "nested"),
        (["x" * 65], "nested"),
        (["bad\x00id"], "nested"),
        ([], 1),
    ],
)
def test_request_validation_rejects_invalid_ids_and_presets(ids: object, preset: object) -> None:
    with pytest.raises(ArchiveInputError):
        ArchiveRequest(ids, preset)


@pytest.mark.parametrize("public_id", ["\ud800", "\udfff", "bad\ud800id", "\ud83d\ude00"])
def test_request_validation_rejects_malformed_unicode_ids(public_id: str) -> None:
    with pytest.raises(ArchiveInputError):
        ArchiveRequest([public_id], "nested")


def test_request_validation_accepts_unicode_scalar_ids() -> None:
    assert ArchiveRequest(["book-\U0001f600"], "nested").ids == ("book-\U0001f600",)


def test_request_mapping_accepts_only_exact_legacy_or_format_aware_shapes() -> None:
    legacy = ArchiveRequest.from_input({"ids": ["book"], "preset": "nested"})
    current = ArchiveRequest.from_input({"ids": ["book"], "preset": "nested", "format": "epub"})

    assert legacy.format == "original"
    assert current.format == "epub"
    invalid_values: tuple[object, ...] = (
        {"ids": [], "preset": "nested", "extra": True},
        {"ids": []},
        {"ids": [], "preset": "nested", "format": "EPUB"},
        {"ids": [], "preset": "nested", "format": ".epub"},
        {"ids": [], "preset": "nested", "format": "mobi"},
        {"ids": [], "preset": "nested", "format": None},
        [],
    )
    for value in invalid_values:
        with pytest.raises(ArchiveInputError):
            ArchiveRequest.from_input(value)


def test_unique_id_limit_is_inclusive_and_applies_after_deduplication() -> None:
    ids = [f"id-{index}" for index in range(MAX_SELECTED_BOOKS)]
    request = ArchiveRequest([*ids, *ids], ArchivePreset.NESTED)
    assert len(request.ids) == MAX_SELECTED_BOOKS

    with pytest.raises(ArchiveLimitError):
        ArchiveRequest([*ids, "one-too-many"], ArchivePreset.NESTED)


def test_extension_is_ascii_casefolded_bounded_and_has_a_fallback() -> None:
    assert normalize_extension(".FB.2_Archive-EXTRA-LONG") == "fb2_archive-extr"
    assert normalize_extension(".фб.?!") == "bin"
    assert len(normalize_extension("A" * 100).encode()) == 16


def test_component_sanitizer_normalizes_replaces_and_handles_reserved_names() -> None:
    decomposed = ' Cafe\u0301 / \\ : * ? " < > | \x00\u200b\n '
    sanitized = sanitize_component(decomposed, "book")

    assert unicodedata.is_normalized("NFC", sanitized)
    assert sanitized == "Café _ _ _ _ _ _ _ _ _ ___"
    assert sanitize_component(" . ", "book") == "book"
    assert sanitize_component("con.txt", "book") == "_con.txt"
    assert sanitize_component("LPT9", "book") == "_LPT9"


@pytest.mark.parametrize("device", ["COM", "LPT"])
@pytest.mark.parametrize("suffix", ["¹", "²", "³"])
@pytest.mark.parametrize("extension", ["", ".txt"])
def test_windows_superscript_device_aliases_are_prefixed(
    device: str, suffix: str, extension: str
) -> None:
    reserved = f"{device}{suffix}{extension}"
    assert sanitize_component(reserved, "book") == f"_{reserved}"


def test_reserved_author_and_title_are_prefixed() -> None:
    book = _book("book", title="NUL", authors=("CON",), series=None)
    assert archive_base_path(book, ArchivePreset.NESTED) == "_CON/_NUL.fb2"


def test_unicode_normalization_and_casefold_create_base_collisions() -> None:
    books = [
        _book("nfc-a", title="Cafe\u0301", series=None),
        _book("nfc-b", title="Café", series=None),
        _book("case-a", title="Straße", series=None),
        _book("case-b", title="STRASSE", series=None),
    ]
    manifest = _manifest([book.public_id for book in books], books)
    members = {member.public_id: member for member in manifest.members}

    assert members["nfc-a"].base_path == members["nfc-b"].base_path
    assert members["case-a"].base_path != members["case-b"].base_path
    assert portable_path_key(members["case-a"].base_path) == portable_path_key(
        members["case-b"].base_path
    )
    assert all(member.collision for member in manifest.members)
    assert len({portable_path_key(member.path) for member in manifest.members}) == 4


def test_sanitizer_created_collision_is_reported_and_disambiguated() -> None:
    books = [
        _book("a", title="A/B", series=None),
        _book("b", title="A\\B", series=None),
    ]
    manifest = _manifest(["a", "b"], books)

    assert [member.base_path for member in manifest.members] == [
        "Last First/A_B.fb2",
        "Last First/A_B.fb2",
    ]
    assert [member.path for member in manifest.members] == [
        "Last First/A_B.fb2",
        "Last First/A_B (2).fb2",
    ]
    assert manifest.entries[0].collision
    assert manifest.entries[0].collision_group == manifest.entries[1].collision_group


@pytest.mark.parametrize(
    ("byte_limit", "expected"),
    [(0, ""), (1, ""), (2, "é"), (3, "é"), (4, "é"), (5, "é界"), (6, "é界a")],
)
def test_utf8_truncation_keeps_the_longest_codepoint_boundary(
    byte_limit: int, expected: str
) -> None:
    assert archive_module._truncate_utf8("é界a", byte_limit) == expected


def test_path_fitting_removes_from_left_component_first_on_byte_ties() -> None:
    fitted = archive_module._fit_parts(archive_module._PathParts(("A" * 200, "B" * 200), "fb2"))

    assert tuple(map(len, fitted.components)) == (119, 116)
    assert len(archive_module._parts_path(fitted).encode()) == MAX_PATH_BYTES


def test_component_and_complete_paths_respect_utf8_byte_limits() -> None:
    book = _book(
        "long",
        title="界" * 200,
        authors=("語" * 200,),
        series="本" * 200,
        original_format="VERYLONGEXTENSION123456",
    )
    path = archive_base_path(book, ArchivePreset.NESTED)

    assert len(path.encode()) <= MAX_PATH_BYTES
    assert all(len(component.encode()) <= MAX_COMPONENT_BYTES for component in path.split("/"))
    assert path.endswith(".verylongextensio")
    assert all(component for component in path.split("/"))


def test_component_truncation_can_create_a_collision() -> None:
    prefix = "界" * 80
    books = [
        _book("a", title=f"{prefix}A", series=None),
        _book("b", title=f"{prefix}B", series=None),
    ]
    manifest = _manifest(["a", "b"], books)

    assert manifest.members[0].base_path == manifest.members[1].base_path
    assert manifest.members[1].path.endswith(" (2).fb2")
    assert len(manifest.members[1].path.encode()) <= MAX_PATH_BYTES


def test_suffix_allocation_skips_every_natural_path() -> None:
    books = [
        _book("a", title="Title", series=None),
        _book("z", title="Title", series=None),
        _book("natural", title="Title (2)", series=None),
    ]
    manifest = _manifest(["z", "natural", "a"], books)
    paths = {member.public_id: member.path for member in manifest.members}

    assert paths == {
        "z": "Last First/Title (3).fb2",
        "natural": "Last First/Title (2).fb2",
        "a": "Last First/Title.fb2",
    }
    assert all(member.collision for member in manifest.members)
    assert all(entry.collision for entry in manifest.entries)
    assert len({member.collision_group for member in manifest.members}) == 1
    assert len({entry.collision_group for entry in manifest.entries}) == 1


def test_collapsed_suffix_families_report_all_allocation_conflicts() -> None:
    prefix = "A" * 192
    books = [
        _book("a-1", title=f"{prefix}aaaa", series=None),
        _book("a-2", title=f"{prefix}aaaa", series=None),
        _book("b-1", title=f"{prefix}bbbb", series=None),
        _book("b-2", title=f"{prefix}bbbb", series=None),
    ]

    ids = [book.public_id for book in books]
    manifest = _manifest(ids, books)
    reversed_manifest = _manifest(list(reversed(ids)), tuple(reversed(books)))

    assert len({portable_path_key(member.path) for member in manifest.members}) == 4
    assert all(member.collision for member in manifest.members)
    assert len({member.collision_group for member in manifest.members}) == 1
    assert {member.public_id: member.path for member in manifest.members} == {
        member.public_id: member.path for member in reversed_manifest.members
    }


def test_large_collision_group_allocates_each_suffix_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book_count = MAX_SELECTED_BOOKS
    ids = [f"book-{index:05d}" for index in range(book_count)]
    books = [_book(public_id, title="Same", series=None) for public_id in ids]
    candidate_attempts = 0
    real_suffix_candidate = archive_module._suffix_candidate

    def counting_suffix_candidate(
        parts: archive_module._PathParts, suffix_number: int
    ) -> tuple[str, str, str]:
        nonlocal candidate_attempts
        candidate_attempts += 1
        return real_suffix_candidate(parts, suffix_number)

    monkeypatch.setattr(archive_module, "_suffix_candidate", counting_suffix_candidate)

    manifest = _manifest(ids, books)

    assert candidate_attempts == book_count - 1
    assert manifest.members[0].path == "Last First/Same.fb2"
    assert manifest.members[-1].path == f"Last First/Same ({book_count}).fb2"


def test_collapsed_suffix_family_uses_indexed_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    family_count = MAX_SELECTED_BOOKS // 2
    prefix = "A" * 192
    books: list[BookSummary] = []
    ids: list[str] = []
    for index in range(family_count):
        title = f"{prefix}{index:04x}"
        for duplicate in range(2):
            public_id = f"book-{index:04x}-{duplicate}"
            ids.append(public_id)
            books.append(_book(public_id, title=title, series=None))

    candidate_attempts = 0
    real_suffix_candidate = archive_module._suffix_candidate

    def counting_suffix_candidate(
        parts: archive_module._PathParts, suffix_number: int
    ) -> tuple[str, str, str]:
        nonlocal candidate_attempts
        candidate_attempts += 1
        return real_suffix_candidate(parts, suffix_number)

    monkeypatch.setattr(archive_module, "_suffix_candidate", counting_suffix_candidate)

    manifest = _manifest(ids, books)

    assert candidate_attempts <= family_count * 6
    assert len({portable_path_key(member.path) for member in manifest.members}) == len(books)
    assert all(member.collision for member in manifest.members)


def test_maximum_manifest_fitting_encodes_each_component_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book_count = MAX_SELECTED_BOOKS
    ids = [f"book-{index:05d}" for index in range(book_count)]
    books = [
        _book(
            public_id,
            title="T" + ("界" * 199),
            authors=(f"{index:05d}" + ("語" * 195),),
            series="S" + ("本" * 199),
        )
        for index, public_id in enumerate(ids)
    ]
    encode_calls = 0
    real_encode_utf8 = archive_module._encode_utf8

    def counting_encode_utf8(value: str) -> bytes:
        nonlocal encode_calls
        encode_calls += 1
        return real_encode_utf8(value)

    monkeypatch.setattr(archive_module, "_encode_utf8", counting_encode_utf8)

    manifest = _manifest(ids, books)

    assert len(manifest.members) == book_count
    assert encode_calls == book_count * 4
    assert all(len(member.path.encode()) <= MAX_PATH_BYTES for member in manifest.members)


def test_public_id_allocation_is_stable_while_preview_order_is_preserved() -> None:
    books = [_book("z", title="Same", series=None), _book("a", title="Same", series=None)]
    first = _manifest(["z", "a"], books)
    second = _manifest(["a", "z"], tuple(reversed(books)))

    assert [entry.public_id for entry in first.entries] == ["z", "a"]
    assert [entry.public_id for entry in second.entries] == ["a", "z"]
    assert (
        {member.public_id: member.path for member in first.members}
        == {member.public_id: member.path for member in second.members}
        == {
            "a": "Last First/Same.fb2",
            "z": "Last First/Same (2).fb2",
        }
    )


def test_manifest_preserves_unknown_and_unavailable_entries() -> None:
    books = [
        _book("active", size=12),
        _book("missed", availability=BookAvailability.MISSED, size=99),
        _book("absent", downloadable=False, size=100),
    ]
    manifest = _manifest(["unknown", "missed", "active", "absent"], books, generation_id=41)

    assert manifest.generation_id == 41
    assert [entry.status for entry in manifest.entries] == [
        ArchiveEntryStatus.UNKNOWN,
        ArchiveEntryStatus.UNAVAILABLE,
        ArchiveEntryStatus.DOWNLOADABLE,
        ArchiveEntryStatus.UNAVAILABLE,
    ]
    assert manifest.entries[0].summary is None
    assert [member.public_id for member in manifest.members] == ["active"]
    assert manifest.total_size == 12


@pytest.mark.parametrize(
    ("target", "expected_statuses", "expected_extensions", "expected_decisions"),
    [
        (
            "original",
            ["downloadable"] * 4,
            [".fb2", ".epub", ".azw3", ".pdf"],
            [OutputDecision.ORIGINAL] * 4,
        ),
        (
            "epub",
            ["downloadable", "downloadable", "unsupported", "unsupported"],
            [".epub", ".epub"],
            [OutputDecision.CONVERT, OutputDecision.PASSTHROUGH],
        ),
        (
            "azw3",
            ["downloadable", "downloadable", "downloadable", "unsupported"],
            [".azw3", ".azw3", ".azw3"],
            [OutputDecision.CONVERT, OutputDecision.CONVERT, OutputDecision.PASSTHROUGH],
        ),
    ],
)
def test_manifest_applies_every_source_target_decision_before_path_allocation(
    target: str,
    expected_statuses: list[str],
    expected_extensions: list[str],
    expected_decisions: list[OutputDecision],
) -> None:
    books = tuple(
        _book(source, original_format=source, size=index + 1, series=None)
        for index, source in enumerate(("fb2", "epub", "azw3", "pdf"))
    )
    conversions = {("fb2", "epub"), ("fb2", "azw3"), ("epub", "azw3")}
    manifest = build_manifest(
        ArchiveRequest([book.public_id for book in books], "nested", target),
        CatalogSummaryBatch(12, books),
        supports_conversion=lambda source, output: (source, output) in conversions,
    )

    assert [entry.status.value for entry in manifest.entries] == expected_statuses
    assert [f".{member.path.rsplit('.', 1)[-1]}" for member in manifest.members] == (
        expected_extensions
    )
    assert [member.decision for member in manifest.members] == expected_decisions
    assert manifest.total_size == sum(member.summary.size for member in manifest.members)
    assert manifest.entries[0].supported_formats == ("epub", "azw3")
    assert manifest.entries[1].supported_formats == ("epub", "azw3")
    assert manifest.entries[2].supported_formats == ("azw3",)
    assert manifest.entries[3].supported_formats == ()


def test_converted_extension_collisions_are_allocated_as_one_target_family() -> None:
    books = (
        _book("fb2", original_format="fb2", title="Same", series=None),
        _book("epub", original_format="epub", title="Same", series=None),
    )
    manifest = build_manifest(
        ArchiveRequest(["fb2", "epub"], "nested", "azw3"),
        CatalogSummaryBatch(3, books),
        supports_conversion=lambda _source, _target: True,
    )

    assert {member.public_id: member.path for member in manifest.members} == {
        "epub": "Last First/Same.azw3",
        "fb2": "Last First/Same (2).azw3",
    }
    assert all(member.collision for member in manifest.members)


def test_decimal_source_size_limit_is_inclusive_and_ignores_omissions() -> None:
    allowed = _book("allowed", size=MAX_ELIGIBLE_SIZE)
    unavailable = _book("unavailable", size=MAX_ELIGIBLE_SIZE, downloadable=False)
    manifest = _manifest(["unknown", "unavailable", "allowed"], [allowed, unavailable])
    assert manifest.total_size == MAX_ELIGIBLE_SIZE

    with pytest.raises(ArchiveLimitError):
        _manifest(
            ["allowed", "extra"],
            [allowed, _book("extra", size=1)],
        )

    with pytest.raises(ArchiveLimitError):
        build_manifest(
            ArchiveRequest(["allowed", "extra"], "nested", "epub"),
            CatalogSummaryBatch(1, (allowed, _book("extra", size=1))),
            supports_conversion=lambda _source, _target: True,
        )


async def test_stateless_service_loads_current_batch_for_each_preview() -> None:
    class FakeCatalog:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        async def bulk_summaries(self, public_ids: Sequence[str]) -> CatalogSummaryBatch:
            self.calls.append(tuple(public_ids))
            generation = len(self.calls)
            return CatalogSummaryBatch(generation, (_book(public_ids[0]),))

    catalog = FakeCatalog()
    service = ArchiveService(catalog, _Acquisition({}))
    request = ArchiveRequest(["book"], "nested")

    assert (await service.preview(request)).generation_id == 1
    assert (await service.preview(request)).generation_id == 2
    assert catalog.calls == [("book",), ("book",)]


async def test_download_stages_exact_zip_paths_content_length_and_zip64(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    books = [
        _book("a", title="First", series=None, size=5),
        _book("b", title="Second", series=None, size=6),
    ]
    catalog = _Catalog((CatalogSummaryBatch(17, tuple(books)),))
    acquisition = _Acquisition({"a": b"alpha", "b": b"second"})
    real_zip_file = zipfile.ZipFile
    init_options: list[tuple[int, bool]] = []
    member_options: list[tuple[str, bool]] = []

    class SpyZipFile:
        def __init__(
            self,
            file: io.BytesIO,
            *,
            mode: Literal["w"],
            compression: int,
            allowZip64: bool,
        ) -> None:
            init_options.append((compression, allowZip64))
            self._archive = real_zip_file(
                file,
                mode=mode,
                compression=compression,
                allowZip64=allowZip64,
            )

        def open(
            self,
            name: str,
            mode: Literal["w"],
            *,
            force_zip64: bool,
        ) -> object:
            member_options.append((name, force_zip64))
            return self._archive.open(name, mode=mode, force_zip64=force_zip64)

        def close(self) -> None:
            self._archive.close()

    monkeypatch.setattr(zipfile, "ZipFile", SpyZipFile)

    staged = await ArchiveService(catalog, acquisition).download(
        ArchiveRequest(["b", "a"], ArchivePreset.FLATTEN)
    )
    payload = await _archive_bytes(staged)

    assert staged.content_length == len(payload)
    assert staged.closed
    assert init_options == [(zipfile.ZIP_DEFLATED, True)]
    assert member_options == [
        ("Last First/First.fb2", True),
        ("Last First/Second.fb2", True),
    ]
    with real_zip_file(io.BytesIO(payload)) as built:
        assert built.namelist() == [
            "Last First/First.fb2",
            "Last First/Second.fb2",
        ]
        assert built.read("Last First/First.fb2") == b"alpha"
        assert built.read("Last First/Second.fb2") == b"second"


async def test_converted_download_uses_target_bytes_extension_and_manifest_generation() -> None:
    book = _book("book", original_format="fb2", size=4, series=None)
    conversion = _Conversion({"book": b"converted"}, {("fb2", "epub")})
    acquisition = _Acquisition({})
    service = ArchiveService(
        _Catalog((CatalogSummaryBatch(23, (book,)),)),
        acquisition,
        conversion,
    )

    payload = await _archive_bytes(
        await service.download(ArchiveRequest(["book"], "nested", "epub"))
    )

    assert conversion.calls == [("book", "epub", 23)]
    assert acquisition.calls == []
    assert conversion.streams[0].closed
    with zipfile.ZipFile(io.BytesIO(payload)) as built:
        assert built.namelist() == ["Last First/Title.epub"]
        assert built.read("Last First/Title.epub") == b"converted"


@pytest.mark.parametrize(("source_format", "target"), [("epub", "epub"), ("azw3", "azw3")])
async def test_same_format_target_passes_original_bytes_with_generation_and_size_integrity(
    source_format: str, target: str
) -> None:
    body = b"unchanged"
    book = _book("book", original_format=source_format, size=len(body), series=None)
    acquisition = _Acquisition({"book": body})
    conversion = _Conversion({}, set())
    staged = await ArchiveService(
        _Catalog((CatalogSummaryBatch(31, (book,)),)), acquisition, conversion
    ).download(ArchiveRequest(["book"], "nested", target))
    payload = await _archive_bytes(staged)

    assert acquisition.calls == [("book", 31)]
    assert conversion.calls == []
    with zipfile.ZipFile(io.BytesIO(payload)) as built:
        assert built.namelist() == [f"Last First/Title.{target}"]
        assert built.read(built.namelist()[0]) == body


async def test_conversion_failure_aborts_and_cleans_the_whole_staged_zip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = _TrackedTemporary()
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **_kwargs: temporary)
    books = (
        _book("first", original_format="fb2", size=1, series=None, title="A"),
        _book("failed", original_format="fb2", size=1, series=None, title="Z"),
    )
    conversion = _Conversion(
        {"first": b"converted", "failed": ConverterExecutionError("failed")},
        {("fb2", "epub")},
    )
    service = ArchiveService(
        _Catalog((CatalogSummaryBatch(7, books),)), _Acquisition({}), conversion
    )

    with pytest.raises(ConverterExecutionError):
        await service.download(ArchiveRequest(["first", "failed"], "nested", "epub"))

    assert temporary.closed
    assert conversion.streams[0].closed


async def test_conversion_source_disappearance_is_the_only_converted_omission() -> None:
    books = (
        _book("gone", original_format="fb2", size=4, series=None),
        _book("kept", original_format="fb2", size=4, series=None),
    )
    conversion = _Conversion(
        {"gone": SourceUnavailableError("gone"), "kept": b"kept"},
        {("fb2", "epub")},
    )
    staged = await ArchiveService(
        _Catalog((CatalogSummaryBatch(19, books),)), _Acquisition({}), conversion
    ).download(ArchiveRequest(["gone", "kept"], "nested", "epub"))
    payload = await _archive_bytes(staged)

    with zipfile.ZipFile(io.BytesIO(payload)) as built:
        assert built.namelist() == ["Last First/Title (2).epub"]
        assert built.read(built.namelist()[0]) == b"kept"


async def test_download_reloads_manifest_and_binds_acquisition_to_current_generation() -> None:
    preview_book = _book("book", title="Preview", series=None, size=3)
    download_book = _book("book", title="Current", series=None, size=3)
    catalog = _Catalog(
        (
            CatalogSummaryBatch(4, (preview_book,)),
            CatalogSummaryBatch(5, (download_book,)),
        )
    )
    acquisition = _Acquisition({"book": b"new"})
    service = ArchiveService(catalog, acquisition)
    request = ArchiveRequest(["book"], "nested")

    assert (await service.preview(request)).members[0].path.endswith("Preview.fb2")
    payload = await _archive_bytes(await service.download(request))

    assert catalog.calls == [("book",), ("book",)]
    assert acquisition.calls == [("book", 5)]
    with zipfile.ZipFile(io.BytesIO(payload)) as built:
        assert built.namelist() == ["Last First/Current.fb2"]


async def test_download_acquires_and_closes_originals_sequentially() -> None:
    active = 0
    maximum = 0

    def opened() -> None:
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)

    def closed() -> None:
        nonlocal active
        active -= 1

    streams = {
        public_id: _Stream((public_id.encode(),), on_open=opened, on_close=closed)
        for public_id in ("c", "a", "b")
    }
    books = tuple(_book(public_id, title=public_id, series=None, size=1) for public_id in streams)
    service = ArchiveService(
        _Catalog((CatalogSummaryBatch(8, books),)),
        _Acquisition(streams, {public_id: 1 for public_id in streams}),
    )

    staged = await service.download(ArchiveRequest(["c", "a", "b"], "nested"))

    assert maximum == 1
    assert active == 0
    await staged.aclose()


@pytest.mark.parametrize(
    "error_type",
    [
        AcquisitionNotFoundError,
        AcquisitionUnavailableError,
        AcquisitionMemberNotFoundError,
    ],
)
async def test_acquire_time_unavailable_members_are_silently_omitted(
    error_type: type[AcquisitionError],
) -> None:
    books = (
        _book("gone", title="Gone", series=None, size=4),
        _book("kept", title="Kept", series=None, size=4),
    )
    acquisition = _Acquisition(
        {"gone": error_type("gone"), "kept": b"kept"},
    )
    staged = await ArchiveService(
        _Catalog((CatalogSummaryBatch(11, books),)), acquisition
    ).download(ArchiveRequest(["gone", "kept"], "nested"))
    payload = await _archive_bytes(staged)

    with zipfile.ZipFile(io.BytesIO(payload)) as built:
        assert built.namelist() == ["Last First/Kept.fb2"]
        assert built.read(built.namelist()[0]) == b"kept"


async def test_all_acquire_time_omissions_raise_no_download_and_close_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = _TrackedTemporary()
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **_kwargs: temporary)
    books = (_book("gone", size=4),)
    service = ArchiveService(
        _Catalog((CatalogSummaryBatch(12, books),)),
        _Acquisition({"gone": AcquisitionUnavailableError("gone")}),
    )

    with pytest.raises(ArchiveNoDownloadsError):
        await service.download(ArchiveRequest(["gone"], "nested"))

    assert temporary.closed


class _TrackedTemporary(io.BytesIO):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    @override
    def close(self) -> None:
        self.close_calls += 1
        super().close()


async def _assert_cancelled[T](task: asyncio.Task[T]) -> None:
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_download_applies_size_limit_from_its_own_catalog_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _Catalog(
        (
            CatalogSummaryBatch(1, (_book("book", size=1),)),
            CatalogSummaryBatch(2, (_book("book", size=MAX_ELIGIBLE_SIZE + 1),)),
        )
    )
    service = ArchiveService(catalog, _Acquisition({"book": b"x"}))
    await service.preview(ArchiveRequest(["book"], "nested"))
    monkeypatch.setattr(
        tempfile,
        "TemporaryFile",
        lambda **_kwargs: pytest.fail("limit failure must precede temporary-file creation"),
    )

    with pytest.raises(ArchiveLimitError):
        await service.download(ArchiveRequest(["book"], "nested"))


@pytest.mark.parametrize(
    "error",
    [
        AcquisitionUnsafePathError("unsafe"),
        AcquisitionAmbiguousMemberError("ambiguous"),
        AcquisitionEncryptedMemberError("encrypted"),
        AcquisitionDirectoryMemberError("directory"),
        AcquisitionSymlinkMemberError("symlink"),
        AcquisitionSizeMismatchError("size"),
        AcquisitionCorruptError("corrupt"),
        AcquisitionSourceIOError("source"),
        AcquisitionStoreShutdownError("shutdown"),
        AcquisitionError("generic acquisition"),
        OSError("generic I/O"),
        zipfile.BadZipFile("generic ZIP"),
    ],
)
async def test_fatal_acquire_errors_abort_and_close_temp(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    temporary = _TrackedTemporary()
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **_kwargs: temporary)
    service = ArchiveService(
        _Catalog((CatalogSummaryBatch(3, (_book("book", size=1),)),)),
        _Acquisition({"book": error}),
    )

    with pytest.raises(type(error)):
        await service.download(ArchiveRequest(["book"], "nested"))

    assert temporary.closed


async def test_unavailable_after_acquisition_is_fatal_and_closes_stream_and_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = _TrackedTemporary()
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **_kwargs: temporary)
    stream = _Stream((b"part",), error=AcquisitionUnavailableError("disappeared"))
    service = ArchiveService(
        _Catalog((CatalogSummaryBatch(3, (_book("book", size=4),)),)),
        _Acquisition({"book": stream}, {"book": 4}),
    )

    with pytest.raises(AcquisitionUnavailableError):
        await service.download(ArchiveRequest(["book"], "nested"))

    assert stream.closed
    assert temporary.closed


async def test_member_writes_are_bounded_even_when_source_yields_a_large_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data = b"x" * (ARCHIVE_CHUNK_SIZE * 3 + 17)
    write_sizes: list[int] = []
    real_zip_file = zipfile.ZipFile

    class TrackingMember:
        def __init__(self, member: object) -> None:
            self._member = member

        def write(self, value: bytes) -> int:
            write_sizes.append(len(value))
            return self._member.write(value)  # type: ignore[attr-defined,no-any-return]

        def close(self) -> None:
            self._member.close()  # type: ignore[attr-defined]

    class TrackingZipFile:
        def __init__(
            self,
            file: io.BytesIO,
            *,
            mode: Literal["w"],
            compression: int,
            allowZip64: bool,
        ) -> None:
            self._archive = real_zip_file(
                file, mode=mode, compression=compression, allowZip64=allowZip64
            )

        def open(self, name: str, mode: Literal["w"], *, force_zip64: bool) -> TrackingMember:
            return TrackingMember(self._archive.open(name, mode=mode, force_zip64=force_zip64))

        def close(self) -> None:
            self._archive.close()

    monkeypatch.setattr(zipfile, "ZipFile", TrackingZipFile)
    staged = await ArchiveService(
        _Catalog((CatalogSummaryBatch(9, (_book("book", size=len(data)),)),)),
        _Acquisition({"book": data}),
    ).download(ArchiveRequest(["book"], "nested"))

    assert write_sizes == [ARCHIVE_CHUNK_SIZE] * 3 + [17]
    await staged.aclose()


@pytest.mark.parametrize("failure_phase", ["write", "member-close", "archive-close"])
async def test_zip_write_and_close_failures_close_original_and_temp(
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
) -> None:
    temporary = _TrackedTemporary()
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **_kwargs: temporary)
    stream = _Stream((b"data",))
    real_zip_file = zipfile.ZipFile

    class FailingMember:
        def __init__(self, member: object) -> None:
            self._member = member

        def write(self, value: bytes) -> int:
            if failure_phase == "write":
                raise OSError("write failed")
            return self._member.write(value)  # type: ignore[attr-defined,no-any-return]

        def close(self) -> None:
            self._member.close()  # type: ignore[attr-defined]
            if failure_phase == "member-close":
                raise OSError("member close failed")

    class FailingZipFile:
        def __init__(
            self,
            file: io.BytesIO,
            *,
            mode: Literal["w"],
            compression: int,
            allowZip64: bool,
        ) -> None:
            self._archive = real_zip_file(
                file, mode=mode, compression=compression, allowZip64=allowZip64
            )

        def open(self, name: str, mode: Literal["w"], *, force_zip64: bool) -> FailingMember:
            return FailingMember(self._archive.open(name, mode=mode, force_zip64=force_zip64))

        def close(self) -> None:
            self._archive.close()
            if failure_phase == "archive-close":
                raise OSError("archive close failed")

    monkeypatch.setattr(zipfile, "ZipFile", FailingZipFile)
    service = ArchiveService(
        _Catalog((CatalogSummaryBatch(3, (_book("book", size=4),)),)),
        _Acquisition({"book": stream}, {"book": 4}),
    )

    with pytest.raises(OSError):
        await service.download(ArchiveRequest(["book"], "nested"))

    assert stream.closed
    assert temporary.closed


async def test_cancellation_during_acquire_closes_temp(monkeypatch: pytest.MonkeyPatch) -> None:
    temporary = _TrackedTemporary()
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **_kwargs: temporary)
    entered = asyncio.Event()
    release = asyncio.Event()
    acquisition = _Acquisition(
        {"book": b"data"},
        acquire_entered=entered,
        acquire_release=release,
    )
    task = asyncio.create_task(
        ArchiveService(
            _Catalog((CatalogSummaryBatch(3, (_book("book", size=4),)),)), acquisition
        ).download(ArchiveRequest(["book"], "nested"))
    )
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert temporary.closed
    assert not acquisition.streams


async def test_cancellation_during_source_iteration_closes_original_and_temp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = _TrackedTemporary()
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **_kwargs: temporary)
    entered = asyncio.Event()
    release = asyncio.Event()
    stream = _Stream((b"data",), read_entered=entered, read_release=release)
    task = asyncio.create_task(
        ArchiveService(
            _Catalog((CatalogSummaryBatch(3, (_book("book", size=4),)),)),
            _Acquisition({"book": stream}, {"book": 4}),
        ).download(ArchiveRequest(["book"], "nested"))
    )
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stream.closed
    assert temporary.closed


async def test_cancellation_waits_for_blocking_write_before_closing_resources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary = _TrackedTemporary()
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **_kwargs: temporary)
    write_entered = threading.Event()
    write_release = threading.Event()
    real_zip_file = zipfile.ZipFile
    stream = _Stream((b"data",))
    member_closed = threading.Event()

    class BlockingMember:
        def __init__(self, member: object) -> None:
            self._member = member

        def write(self, value: bytes) -> int:
            write_entered.set()
            write_release.wait()
            return self._member.write(value)  # type: ignore[attr-defined,no-any-return]

        def close(self) -> None:
            self._member.close()  # type: ignore[attr-defined]
            member_closed.set()

    class BlockingZipFile:
        def __init__(
            self,
            file: io.BytesIO,
            *,
            mode: Literal["w"],
            compression: int,
            allowZip64: bool,
        ) -> None:
            self._archive = real_zip_file(
                file, mode=mode, compression=compression, allowZip64=allowZip64
            )

        def open(self, name: str, mode: Literal["w"], *, force_zip64: bool) -> BlockingMember:
            return BlockingMember(self._archive.open(name, mode=mode, force_zip64=force_zip64))

        def close(self) -> None:
            self._archive.close()

    monkeypatch.setattr(zipfile, "ZipFile", BlockingZipFile)
    task = asyncio.create_task(
        ArchiveService(
            _Catalog((CatalogSummaryBatch(3, (_book("book", size=4),)),)),
            _Acquisition({"book": stream}, {"book": 4}),
        ).download(ArchiveRequest(["book"], "nested"))
    )
    assert await asyncio.to_thread(write_entered.wait, 2)

    task.cancel()
    await asyncio.sleep(0)
    assert not temporary.closed
    assert not member_closed.is_set()

    write_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert member_closed.is_set()
    assert stream.closed
    assert temporary.closed


async def test_staged_archive_is_single_use_and_explicit_close_is_idempotent() -> None:
    staged = await ArchiveService(
        _Catalog((CatalogSummaryBatch(3, (_book("book", size=4),)),)),
        _Acquisition({"book": b"data"}),
    ).download(ArchiveRequest(["book"], "nested"))
    iterator = cast(AsyncGenerator[bytes], staged.__aiter__())
    assert await anext(iterator)

    await iterator.aclose()
    assert staged.closed
    await staged.aclose()
    await staged.aclose()
    with pytest.raises(RuntimeError, match="single-use"):
        staged.__aiter__()


async def test_staged_close_waits_through_cancellation_and_can_be_repeated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_entered = threading.Event()
    close_release = threading.Event()

    class BlockingCloseTemporary(_TrackedTemporary):
        @override
        def close(self) -> None:
            if not self.closed:
                close_entered.set()
                close_release.wait()
            super().close()

    temporary = BlockingCloseTemporary()
    monkeypatch.setattr(tempfile, "TemporaryFile", lambda **_kwargs: temporary)
    staged = await ArchiveService(
        _Catalog((CatalogSummaryBatch(3, (_book("book", size=4),)),)),
        _Acquisition({"book": b"data"}),
    ).download(ArchiveRequest(["book"], "nested"))
    close_task = asyncio.create_task(staged.aclose())
    assert await asyncio.to_thread(close_entered.wait, 2)

    close_task.cancel()
    await asyncio.sleep(0)
    assert not close_task.done()
    assert temporary.close_calls == 0

    close_release.set()
    await _assert_cancelled(close_task)
    assert temporary.closed
    assert staged.closed

    await staged.aclose()
    assert temporary.close_calls == 1
