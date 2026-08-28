"""Selected-book request, manifest, and portable archive-path tests."""

import unicodedata
from collections.abc import Sequence

import pytest

import sopds.acquisition.archive as archive_module
from sopds.acquisition.archive import (
    MAX_COMPONENT_BYTES,
    MAX_ELIGIBLE_SIZE,
    MAX_PATH_BYTES,
    MAX_SELECTED_BOOKS,
    ArchiveEntryStatus,
    ArchiveInputError,
    ArchiveLimitError,
    ArchiveManifest,
    ArchivePreset,
    ArchiveRequest,
    ArchiveService,
    archive_base_path,
    build_manifest,
    normalize_extension,
    portable_path_key,
    sanitize_component,
)
from sopds.catalog.contracts import (
    BookAvailability,
    BookSummary,
    CatalogSummaryBatch,
)


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


def test_request_mapping_rejects_unknown_or_missing_fields() -> None:
    with pytest.raises(ArchiveInputError):
        ArchiveRequest.from_input({"ids": [], "preset": "nested", "extra": True})
    with pytest.raises(ArchiveInputError):
        ArchiveRequest.from_input({"ids": []})
    with pytest.raises(ArchiveInputError):
        ArchiveRequest.from_input([])


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


async def test_stateless_service_loads_current_batch_for_each_preview() -> None:
    class FakeCatalog:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        async def bulk_summaries(self, public_ids: Sequence[str]) -> CatalogSummaryBatch:
            self.calls.append(tuple(public_ids))
            generation = len(self.calls)
            return CatalogSummaryBatch(generation, (_book(public_ids[0]),))

    catalog = FakeCatalog()
    service = ArchiveService(catalog)
    request = ArchiveRequest(["book"], "nested")

    assert (await service.preview(request)).generation_id == 1
    assert (await service.preview(request)).generation_id == 2
    assert catalog.calls == [("book",), ("book",)]
