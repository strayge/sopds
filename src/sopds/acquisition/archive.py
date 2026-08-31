"""Authoritative manifest generation and staged ZIP construction."""

import asyncio
import os
import tempfile
import unicodedata
import zipfile
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import IO, BinaryIO, Protocol, Self

from sopds.acquisition.contracts import (
    AcquisitionMemberNotFoundError,
    AcquisitionNotFoundError,
    AcquisitionSizeMismatchError,
    AcquisitionTarget,
    AcquisitionUnavailableError,
    AsyncByteStream,
    BulkAcquisition,
)
from sopds.catalog.contracts import (
    BookAvailability,
    BookSummary,
    CatalogSummaryBatch,
)
from sopds.conversion.contracts import ConversionResult, SourceUnavailableError
from sopds.conversion.policy import OUTPUT_POLICY, OutputDecision, OutputPolicy

MAX_SELECTED_BOOKS = 10_000
MAX_ELIGIBLE_SIZE = 10_000_000_000
MAX_PUBLIC_ID_CHARS = 64
MAX_COMPONENT_BYTES = 200
MAX_PATH_BYTES = 240
MAX_EXTENSION_BYTES = 16
ARCHIVE_CHUNK_SIZE = 64 * 1024

_WINDOWS_ILLEGAL = frozenset('/\\:*?"<>|')
_WINDOWS_RESERVED = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {
        f"{device}{suffix}"
        for device in ("COM", "LPT")
        for suffix in (*map(str, range(1, 10)), "¹", "²", "³")
    }
)


class ArchiveError(Exception):
    """Keep archive failures distinct from acquisition and catalog errors."""


class ArchiveInputError(ArchiveError, ValueError):
    """Reject malformed selected-book requests with a bounded public message."""


class ArchiveLimitError(ArchiveInputError):
    """Reject requests exceeding an authoritative item or source-size limit."""


class ArchiveNoDownloadsError(ArchiveInputError):
    """Prevent a download response from containing an empty archive."""


class ArchivePreset(StrEnum):
    NESTED = "nested"
    FLATTEN = "flatten"
    LIST = "list"


class ArchiveEntryStatus(StrEnum):
    DOWNLOADABLE = "downloadable"
    UNSUPPORTED = "unsupported"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True, init=False)
class ArchiveRequest:
    ids: tuple[str, ...]
    preset: ArchivePreset
    format: str

    def __init__(self, ids: object, preset: object, format: object = "original") -> None:  # noqa: A002
        normalized_ids = _validate_ids(ids)
        if not isinstance(preset, (str, ArchivePreset)):
            raise ArchiveInputError("Invalid archive preset")
        try:
            normalized_preset = ArchivePreset(preset)
        except ValueError as error:
            raise ArchiveInputError("Invalid archive preset") from error
        if not isinstance(format, str):
            raise ArchiveInputError("Invalid archive format")
        try:
            normalized_format = OUTPUT_POLICY.choice(format).key
        except ValueError as error:
            raise ArchiveInputError("Invalid archive format") from error
        if format != normalized_format:
            raise ArchiveInputError("Invalid archive format")
        object.__setattr__(self, "ids", normalized_ids)
        object.__setattr__(self, "preset", normalized_preset)
        object.__setattr__(self, "format", normalized_format)

    @classmethod
    def from_input(cls, value: object) -> Self:
        if not isinstance(value, Mapping) or set(value) not in (
            {"ids", "preset"},
            {"ids", "preset", "format"},
        ):
            raise ArchiveInputError("Invalid archive request")
        return cls(value["ids"], value["preset"], value.get("format", "original"))


@dataclass(frozen=True, slots=True)
class ArchivePreviewEntry:
    public_id: str
    summary: BookSummary | None
    status: ArchiveEntryStatus
    collision: bool = False
    collision_group: str | None = None
    supported_formats: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    public_id: str
    summary: BookSummary
    base_path: str
    path: str
    collision: bool = False
    collision_group: str | None = None
    decision: OutputDecision = OutputDecision.ORIGINAL


@dataclass(frozen=True, slots=True)
class ArchiveManifest:
    request: ArchiveRequest
    generation_id: int | None
    entries: tuple[ArchivePreviewEntry, ...]
    members: tuple[ArchiveMember, ...]
    total_size: int


class BulkCatalog(Protocol):
    async def bulk_summaries(self, public_ids: Sequence[str]) -> CatalogSummaryBatch: ...


class FormatConversion(Protocol):
    def supports(self, source_format: str, target_format: str) -> bool: ...

    async def convert(
        self,
        public_id: str,
        target_format: str,
        *,
        expected_generation_id: int | None = None,
    ) -> ConversionResult: ...


class _OwnedArchiveInput(Protocol):
    @property
    def content_length(self) -> int: ...

    @property
    def stream(self) -> AsyncByteStream: ...


class StagedArchive:
    """Own one seekable temporary ZIP until consumed or explicitly closed."""

    def __init__(self, file: BinaryIO, content_length: int) -> None:
        self.content_length = content_length
        self._file = file
        self._iteration_started = False
        self._closed = False
        self._io_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def closed(self) -> bool:
        return self._closed

    def __aiter__(self) -> AsyncIterator[bytes]:
        if self._iteration_started:
            raise RuntimeError("Staged archive streams are single-use")
        self._iteration_started = True
        return self._iterate()

    async def _iterate(self) -> AsyncIterator[bytes]:
        try:
            while chunk := await self._read():
                yield chunk
        finally:
            await self.aclose()

    async def _read(self) -> bytes:
        async with self._io_lock:
            if self._closed:
                return b""
            return await _blocking(partial(self._file.read, ARCHIVE_CHUNK_SIZE))

    async def _close(self) -> None:
        async with self._io_lock:
            if self._closed:
                return
            try:
                await _blocking(self._file.close)
            finally:
                self._closed = True

    async def aclose(self) -> None:
        """Close once, waiting through cancellation before preserving it."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await _wait_owned_task(self._close_task)


class ArchiveService:
    """Resolve and acquire each request afresh without retaining preview state."""

    def __init__(
        self,
        catalog: BulkCatalog,
        acquisition: BulkAcquisition,
        conversion: FormatConversion | None = None,
        output_policy: OutputPolicy = OUTPUT_POLICY,
    ) -> None:
        self._catalog = catalog
        self._acquisition = acquisition
        self._conversion = conversion
        self._output_policy = output_policy

    def _manifest(self, request: ArchiveRequest, batch: CatalogSummaryBatch) -> ArchiveManifest:
        return build_manifest(
            request,
            batch,
            output_policy=self._output_policy,
            supports_conversion=(
                self._conversion.supports if self._conversion is not None else None
            ),
        )

    async def preview(self, request: ArchiveRequest) -> ArchiveManifest:
        batch = await self._catalog.bulk_summaries(request.ids)
        return self._manifest(request, batch)

    async def download(self, request: ArchiveRequest) -> StagedArchive:
        """Rebuild current metadata and transfer the completed temporary ZIP to its caller."""
        batch = await self._catalog.bulk_summaries(request.ids)
        manifest = self._manifest(request, batch)
        if not manifest.members:
            raise ArchiveNoDownloadsError("No selected books are available for download")

        staged_file = await _blocking(
            lambda: tempfile.TemporaryFile(mode="w+b"),  # noqa: SIM115
            cancel_cleanup=lambda opened: opened.close(),
        )
        primary: BaseException | None = None
        staged: StagedArchive | None = None
        try:
            staged = await self._build(manifest, staged_file)
        except BaseException as error:
            primary = error
        cleanup = (
            await _capture_cleanup(partial(_blocking, staged_file.close))
            if staged is None
            else None
        )
        _raise_after_cleanup(primary, cleanup)
        if staged is None:
            raise AssertionError("Successful archive build did not transfer ownership")
        return staged

    async def _build(self, manifest: ArchiveManifest, staged_file: BinaryIO) -> StagedArchive:
        archive = await _blocking(
            lambda: zipfile.ZipFile(
                staged_file,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
                allowZip64=True,
            ),
            cancel_cleanup=lambda opened: opened.close(),
        )
        primary: BaseException | None = None
        included = 0
        try:
            original_ids = tuple(
                member.public_id
                for member in manifest.members
                if member.decision in {OutputDecision.ORIGINAL, OutputDecision.PASSTHROUGH}
            )
            prepared_targets = (
                await self._acquisition.resolve_targets(
                    original_ids,
                    expected_generation_id=manifest.generation_id,
                )
                if original_ids
                else {}
            )
            for member in sorted(manifest.members, key=lambda candidate: candidate.path):
                try:
                    source = await self._acquire_member(manifest, member, prepared_targets)
                except (
                    AcquisitionNotFoundError,
                    AcquisitionUnavailableError,
                    AcquisitionMemberNotFoundError,
                    SourceUnavailableError,
                ):
                    continue
                await _add_member(
                    archive,
                    member,
                    source,
                    expected_source_size=(
                        member.summary.size
                        if member.decision in {OutputDecision.ORIGINAL, OutputDecision.PASSTHROUGH}
                        else None
                    ),
                )
                included += 1
            if not included:
                raise ArchiveNoDownloadsError("No selected books are available for download")
        except BaseException as error:
            primary = error

        cleanup = await _capture_cleanup(partial(_blocking, archive.close))
        _raise_after_cleanup(primary, cleanup)
        content_length = await _blocking(partial(_rewind_and_measure, staged_file))
        return StagedArchive(staged_file, content_length)

    async def _acquire_member(
        self,
        manifest: ArchiveManifest,
        member: ArchiveMember,
        prepared_targets: Mapping[str, AcquisitionTarget],
    ) -> _OwnedArchiveInput:
        if member.decision in {OutputDecision.ORIGINAL, OutputDecision.PASSTHROUGH}:
            target = prepared_targets.get(member.public_id)
            if target is None:
                raise AcquisitionNotFoundError("Original is unavailable")
            return await self._acquisition.acquire_target(target)
        if member.decision is OutputDecision.CONVERT and self._conversion is not None:
            return await self._conversion.convert(
                member.public_id,
                manifest.request.format,
                expected_generation_id=manifest.generation_id,
            )
        raise AssertionError("Archive manifest contains an unsupported output member")


async def _blocking[T](
    function: Callable[[], T],
    *,
    cancel_cleanup: Callable[[T], None] | None = None,
) -> T:
    """Drain a worker before cancellation can release an object it is using."""
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError as cancellation:
        try:
            result = await _drain_task(task)
        except BaseException:
            raise cancellation from None
        if cancel_cleanup is not None:
            cleanup = asyncio.create_task(asyncio.to_thread(cancel_cleanup, result))
            with suppress(BaseException):
                await _drain_task(cleanup)
        raise cancellation


async def _drain_task[T](task: asyncio.Task[T]) -> T:
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    return task.result()


async def _wait_owned_task[T](task: asyncio.Task[T]) -> T:
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
        except BaseException:
            break
    if cancellation is not None:
        if not task.cancelled():
            task.exception()
        raise cancellation
    return task.result()


async def _capture_cleanup(operation: Callable[[], Awaitable[object]]) -> BaseException | None:
    try:
        await operation()
    except BaseException as error:
        return error
    return None


async def _close_async(operation: Callable[[], Awaitable[None]]) -> None:
    async def run() -> None:
        await operation()

    task = asyncio.create_task(run())
    await _wait_owned_task(task)


def _raise_after_cleanup(
    primary: BaseException | None,
    cleanup: BaseException | None,
) -> None:
    if isinstance(primary, asyncio.CancelledError):
        raise primary
    if isinstance(cleanup, asyncio.CancelledError):
        raise cleanup
    if primary is not None:
        raise primary
    if cleanup is not None:
        raise cleanup


async def _add_member(
    archive: zipfile.ZipFile,
    member: ArchiveMember,
    source: _OwnedArchiveInput,
    *,
    expected_source_size: int | None,
) -> None:
    primary: BaseException | None = None
    try:
        if expected_source_size is not None and source.content_length != expected_source_size:
            raise AcquisitionSizeMismatchError("Original size does not match catalog metadata")
        await _write_member(archive, member.path, source)
    except BaseException as error:
        primary = error
    cleanup = await _capture_cleanup(partial(_close_async, source.stream.aclose))
    _raise_after_cleanup(primary, cleanup)


async def _write_member(
    archive: zipfile.ZipFile,
    path: str,
    source: _OwnedArchiveInput,
) -> None:
    output = await _blocking(
        lambda: archive.open(path, mode="w", force_zip64=True),
        cancel_cleanup=lambda opened: opened.close(),
    )
    primary: BaseException | None = None
    written_total = 0
    try:
        async for chunk in source.stream:
            for offset in range(0, len(chunk), ARCHIVE_CHUNK_SIZE):
                part = chunk[offset : offset + ARCHIVE_CHUNK_SIZE]
                written = await _blocking(partial(output.write, part))
                if written != len(part):
                    raise OSError("Short archive member write")
                written_total += written
        if written_total != source.content_length:
            raise AcquisitionSizeMismatchError("Archive input stream size does not match metadata")
    except BaseException as error:
        primary = error
    cleanup = await _capture_cleanup(partial(_blocking, output.close))
    _raise_after_cleanup(primary, cleanup)


def _rewind_and_measure(file: IO[bytes]) -> int:
    file.seek(0, os.SEEK_END)
    content_length = file.tell()
    file.seek(0)
    return content_length


def _validate_ids(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ArchiveInputError("Invalid selected book IDs")
    unique: dict[str, None] = {}
    for public_id in value:
        if (
            not isinstance(public_id, str)
            or not public_id
            or len(public_id) > MAX_PUBLIC_ID_CHARS
            or "\x00" in public_id
        ):
            raise ArchiveInputError("Invalid public book ID")
        try:
            public_id.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ArchiveInputError("Invalid public book ID") from error
        unique.setdefault(public_id, None)
        if len(unique) > MAX_SELECTED_BOOKS:
            raise ArchiveLimitError("Too many selected books")
    return tuple(unique)


def normalize_extension(value: str) -> str:
    extension = "".join(
        character.casefold()
        for character in value.lstrip(".")
        if character.isascii() and (character.isalnum() or character in "_-")
    )
    return extension[:MAX_EXTENSION_BYTES] or "bin"


def sanitize_component(value: str, fallback: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    replaced = "".join(
        "_"
        if character in _WINDOWS_ILLEGAL
        or character == "\x00"
        or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        else character
        for character in normalized
    )
    sanitized = " ".join(replaced.split()).strip(" .") or fallback
    if _is_windows_reserved(sanitized):
        sanitized = f"_{sanitized}"
    return unicodedata.normalize("NFC", sanitized)


def format_series_number(value: str | None) -> str:
    if value is None:
        return ""
    sanitized = sanitize_component(value, "").strip(" .")
    if not sanitized:
        return ""
    return sanitized.zfill(2) if sanitized.isdecimal() else sanitized


def portable_path_key(path: str) -> str:
    return unicodedata.normalize("NFC", path).casefold()


def build_manifest(
    request: ArchiveRequest,
    batch: CatalogSummaryBatch,
    *,
    output_policy: OutputPolicy = OUTPUT_POLICY,
    supports_conversion: Callable[[str, str], bool] | None = None,
) -> ArchiveManifest:
    known = {book.public_id: book for book in batch.books}
    selected: list[tuple[str, BookSummary | None, ArchiveEntryStatus, tuple[str, ...]]] = []
    downloadable: list[tuple[BookSummary, OutputDecision]] = []
    total_size = 0

    def is_supported(source_format: str, target_format: str) -> bool:
        decision = output_policy.decision(source_format, target_format)
        if decision is OutputDecision.CONVERT:
            return supports_conversion is not None and supports_conversion(
                source_format, target_format
            )
        return decision is not OutputDecision.UNSUPPORTED

    for public_id in request.ids:
        book = known.get(public_id)
        if book is None:
            selected.append((public_id, None, ArchiveEntryStatus.UNKNOWN, ()))
            continue
        if not book.downloadable or book.availability is BookAvailability.MISSED:
            selected.append((public_id, book, ArchiveEntryStatus.UNAVAILABLE, ()))
            continue

        supported_formats = tuple(
            choice.key
            for choice in output_policy.choices()
            if choice.key != "original" and is_supported(book.original_format, choice.key)
        )
        decision = output_policy.decision(book.original_format, request.format)
        if decision is OutputDecision.CONVERT and not is_supported(
            book.original_format, request.format
        ):
            decision = OutputDecision.UNSUPPORTED
        if decision is OutputDecision.UNSUPPORTED:
            selected.append((public_id, book, ArchiveEntryStatus.UNSUPPORTED, supported_formats))
            continue

        selected.append((public_id, book, ArchiveEntryStatus.DOWNLOADABLE, supported_formats))
        downloadable.append((book, decision))
        total_size += book.size
        if total_size > MAX_ELIGIBLE_SIZE:
            raise ArchiveLimitError("Selected books exceed the source-size limit")

    target_choice = output_policy.choice(request.format)
    path_records = [
        _PathRecord(
            index,
            book,
            decision,
            _book_path_parts(
                book,
                request.preset,
                output_extension=(
                    target_choice.extension if request.format != "original" else None
                ),
            ),
        )
        for index, (book, decision) in enumerate(downloadable)
    ]
    base_groups: dict[str, list[_PathRecord]] = {}
    natural_owners: dict[str, list[int]] = {}
    for record in path_records:
        record.base_parts = _fit_parts(record.base_parts)
        record.base_path = _parts_path(record.base_parts)
        record.base_key = portable_path_key(record.base_path)
        base_groups.setdefault(record.base_key, []).append(record)
        natural_owners.setdefault(record.base_key, []).append(record.index)

    collisions = _CollisionSets(len(path_records))
    allocated_keys = set(natural_owners)
    generated_owners: dict[str, int] = {}
    next_suffix_by_family: dict[str, tuple[int, int]] = {}
    for base_key in sorted(base_groups):
        group = sorted(base_groups[base_key], key=lambda record: record.book.public_id)
        for record in group[1:]:
            collisions.union(group[0].index, record.index)
        group[0].path = group[0].base_path

        suffix_number = 2
        for record in group[1:]:
            while True:
                candidate, candidate_key, family_key = _suffix_candidate(
                    record.base_parts, suffix_number
                )
                indexed_suffix, indexed_owner = next_suffix_by_family.get(
                    family_key, (suffix_number, record.index)
                )
                if indexed_suffix > suffix_number:
                    collisions.union(record.index, indexed_owner)
                    suffix_number = indexed_suffix
                    continue

                next_suffix_by_family[family_key] = (suffix_number + 1, record.index)
                suffix_number += 1
                if candidate_key in allocated_keys:
                    for owner in natural_owners.get(candidate_key, ()):
                        collisions.union(record.index, owner)
                    generated_owner = generated_owners.get(candidate_key)
                    if generated_owner is not None:
                        collisions.union(record.index, generated_owner)
                    continue

                allocated_keys.add(candidate_key)
                generated_owners[candidate_key] = record.index
                record.path = candidate
                break

    collision_groups = collisions.groups(path_records)
    for record in path_records:
        record.collision_group = collision_groups.get(record.index)

    records_by_id = {record.book.public_id: record for record in path_records}
    entries = tuple(
        ArchivePreviewEntry(
            public_id,
            book,
            status,
            collision=(
                status is ArchiveEntryStatus.DOWNLOADABLE
                and records_by_id[public_id].collision_group is not None
            ),
            collision_group=(
                records_by_id[public_id].collision_group
                if status is ArchiveEntryStatus.DOWNLOADABLE
                else None
            ),
            supported_formats=supported_formats,
        )
        for public_id, book, status, supported_formats in selected
    )
    members = tuple(
        ArchiveMember(
            record.book.public_id,
            record.book,
            record.base_path,
            record.path,
            decision=record.decision,
            collision=record.collision_group is not None,
            collision_group=record.collision_group,
        )
        for record in path_records
    )
    return ArchiveManifest(request, batch.generation_id, entries, members, total_size)


@dataclass(frozen=True, slots=True)
class _PathParts:
    components: tuple[str, ...]
    extension: str


@dataclass(slots=True)
class _PathRecord:
    index: int
    book: BookSummary
    decision: OutputDecision
    base_parts: _PathParts
    base_path: str = ""
    base_key: str = ""
    path: str = ""
    collision_group: str | None = None


class _CollisionSets:
    def __init__(self, size: int) -> None:
        self._parents = list(range(size))
        self._sizes = [1] * size

    def find(self, index: int) -> int:
        while self._parents[index] != index:
            self._parents[index] = self._parents[self._parents[index]]
            index = self._parents[index]
        return index

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root == right_root:
            return
        if self._sizes[left_root] < self._sizes[right_root]:
            left_root, right_root = right_root, left_root
        self._parents[right_root] = left_root
        self._sizes[left_root] += self._sizes[right_root]

    def groups(self, records: Sequence[_PathRecord]) -> dict[int, str]:
        members: dict[int, list[_PathRecord]] = {}
        for record in records:
            members.setdefault(self.find(record.index), []).append(record)

        result: dict[int, str] = {}
        for group in members.values():
            if len(group) < 2:
                continue
            group_key = min(record.base_key for record in group)
            result.update((record.index, group_key) for record in group)
        return result


def _book_path_parts(
    book: BookSummary,
    preset: ArchivePreset,
    *,
    output_extension: str | None = None,
) -> _PathParts:
    author_source = book.authors[0] if book.authors else ""
    author = sanitize_component(
        " ".join(part.strip() for part in author_source.split(",") if part.strip()),
        "Unknown author",
    )
    title = sanitize_component(book.title, "book")
    extension = normalize_extension(output_extension or book.original_format)

    if book.series is None:
        components: tuple[str, ...] = (
            (f"{author}. {title}",) if preset is ArchivePreset.LIST else (author, title)
        )
        return _PathParts(_sanitize_assembled(components), extension)

    series = sanitize_component(book.series, "Series")
    number = format_series_number(book.series_number)
    series_with_number = " ".join(part for part in (series, number) if part)
    if preset is ArchivePreset.NESTED:
        filename = f"{number} - {title}" if number else title
        components = (author, series, filename)
    elif preset is ArchivePreset.FLATTEN:
        components = (author, f"{series_with_number} - {title}")
    else:
        components = (f"{author}. {series_with_number} - {title}",)
    return _PathParts(_sanitize_assembled(components), extension)


def _sanitize_assembled(components: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sanitize_component(component, "book") for component in components)


def _fit_parts(parts: _PathParts, suffix: str = "") -> _PathParts:
    reservation = f"{suffix}.{parts.extension}"
    reservation_bytes = len(_encode_utf8(reservation))
    components: list[str] = []
    byte_lengths: list[int] = []
    last_index = len(parts.components) - 1
    for index, component in enumerate(parts.components):
        budget = MAX_COMPONENT_BYTES
        if index == last_index:
            budget -= reservation_bytes
        fitted, byte_length = _truncate_utf8_with_length(component, budget)
        components.append(fitted)
        byte_lengths.append(byte_length)

    rendered_lengths = byte_lengths.copy()
    rendered_lengths[-1] += reservation_bytes
    path_bytes = sum(rendered_lengths) + last_index
    if path_bytes <= MAX_PATH_BYTES:
        return _PathParts(tuple(components), parts.extension)

    characters = [list(component) for component in components]
    while path_bytes > MAX_PATH_BYTES:
        candidates = [
            (rendered_lengths[index], index)
            for index, component in enumerate(characters)
            if len(component) > 1
        ]
        if not candidates:
            raise AssertionError("Archive path cannot fit within the portable byte limit")
        longest = max(length for length, _index in candidates)
        index = next(index for length, index in candidates if length == longest)

        removed_bytes = _codepoint_utf8_size(characters[index].pop())
        while characters[index] and characters[index][-1] in " .":
            removed_bytes += 1
            characters[index].pop()
        if not characters[index]:
            raise AssertionError("Archive path fitting removed a required component")
        rendered_lengths[index] -= removed_bytes
        path_bytes -= removed_bytes

    return _PathParts(tuple("".join(component) for component in characters), parts.extension)


def _suffix_candidate(parts: _PathParts, suffix_number: int) -> tuple[str, str, str]:
    suffix = f" ({suffix_number})"
    fitted = _fit_parts(parts, suffix=suffix)
    candidate = _parts_path(fitted, suffix=suffix)
    candidate_key = portable_path_key(candidate)
    family_key = portable_path_key(_parts_path(fitted))
    return candidate, candidate_key, family_key


def _parts_path(parts: _PathParts, suffix: str = "") -> str:
    components = [*parts.components]
    components[-1] = f"{components[-1]}{suffix}.{parts.extension}"
    return "/".join(components)


def _truncate_utf8_with_length(value: str, byte_limit: int) -> tuple[str, int]:
    encoded = _encode_utf8(value)
    if len(encoded) <= byte_limit:
        return value, len(encoded)
    end = max(byte_limit, 0)
    while end and encoded[end] & 0xC0 == 0x80:
        end -= 1
    decoded = encoded[:end].decode("utf-8")
    truncated = decoded.rstrip(" .")
    stripped_bytes = end - (len(decoded) - len(truncated))
    return truncated, stripped_bytes


def _encode_utf8(value: str) -> bytes:
    return value.encode("utf-8")


def _codepoint_utf8_size(value: str) -> int:
    codepoint = ord(value)
    if codepoint <= 0x7F:
        return 1
    if codepoint <= 0x7FF:
        return 2
    if codepoint <= 0xFFFF:
        return 3
    return 4


def _is_windows_reserved(value: str) -> bool:
    stem = value.split(".", 1)[0].rstrip(" ").upper()
    return stem in _WINDOWS_RESERVED
