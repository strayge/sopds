"""Streaming generation import and atomic activation orchestration."""

import asyncio
import base64
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from time import perf_counter

from asyncpg import PostgresError  # type: ignore[import-untyped]
from tortoise.exceptions import BaseORMException

from sopds.catalog.genre_names import genre_label
from sopds.catalog.search import (
    bound_search_projection,
    normalize_search_projection,
    normalize_text,
)
from sopds.db.repository import DEFAULT_BATCH_SIZE, CatalogRepository, IdCounters
from sopds.db.rows import (
    ArchiveRow,
    AuthorRow,
    BookAuthorRow,
    BookGenreRow,
    BookRow,
    BookSearchRow,
    CatalogWriteBatch,
    GenreRow,
    SeriesRow,
)
from sopds.imports.availability import archive_availability
from sopds.imports.fingerprint import SourceFingerprint, hash_source
from sopds.imports.inpx import (
    InpxParserError,
    InpxRecord,
    InpxRecordIterator,
    InpxRecordRejection,
    parse_inpx,
)
from sopds.imports.status import ImportOutcome, ImportResult, ImportState, ImportTrigger

_LOGGER = logging.getLogger(__name__)
_PROGRESS_RECORD_INTERVAL = 100_000
_MAX_REJECTION_DETAIL_LOGS = 10
_MAX_BIGINT = 2**63 - 1


def _log_import_terminal(
    trigger: ImportTrigger,
    outcome: ImportOutcome,
    started: float,
    counters: list[int],
    *,
    run_id: int | None = None,
    generation_id: int | None = None,
    failure_type: str | None = None,
) -> None:
    """Emit duration only after outcome finalization and owned worker cleanup."""
    duration_ms = int((perf_counter() - started) * 1000)
    identity = f" run_id={run_id} generation_id={generation_id}" if run_id is not None else ""
    failure = f" failure_type={failure_type}" if failure_type is not None else ""
    message = (
        f"Catalog import finished phase=import trigger={trigger.value} "
        f"outcome={outcome.value} duration_ms={duration_ms}{identity}{failure} "
        f"read={counters[0]} imported={counters[1]} deleted={counters[2]} "
        f"rejected={counters[3]}"
    )
    if outcome is ImportOutcome.IMPORTED:
        _LOGGER.info(message)
    else:
        _LOGGER.warning(message)


class SourceChangedError(RuntimeError):
    """Prevents a catalog assembled from a changing source from becoming visible."""


class CatalogDataError(ValueError):
    """Identifies invalid mapped metadata without retaining raw record contents."""


def _log_record_rejection(
    run_id: int, rejection_number: int, rejection: InpxRecordRejection
) -> None:
    if rejection_number > _MAX_REJECTION_DETAIL_LOGS:
        return
    _LOGGER.warning(
        f"Catalog record rejected phase=staging run_id={run_id} "
        f"source_entry={rejection.source_entry!r} line_number={rejection.line_number} "
        f"reason={rejection.reason!r}"
    )


def _validate_import_counters(counters: list[int]) -> None:
    records_read, imported, deleted, rejected = counters
    if records_read != imported + deleted + rejected:
        raise CatalogDataError("Import counters failed structural validation")
    valid_records = imported + deleted
    if records_read and valid_records == 0:
        raise CatalogDataError("Catalog source contains no valid records")
    allowed_rejections = max(1, records_read // 10)
    if rejected > allowed_rejections:
        raise CatalogDataError(
            f"Catalog rejected {rejected} of {records_read} records, exceeding the 10% limit"
        )


class _ParserWorker:
    """Pull bounded record batches through exactly one dedicated parser thread."""

    def __init__(self, path: Path, batch_size: int) -> None:
        self._path = path
        self._batch_size = batch_size
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sopds-inpx")
        self._records: InpxRecordIterator | None = None
        self._close_task: asyncio.Task[None] | None = None

    def _next_batch(self) -> list[InpxRecord | InpxRecordRejection]:
        if self._records is None:
            self._records = parse_inpx(self._path)
        batch: list[InpxRecord | InpxRecordRejection] = []
        for _ in range(self._batch_size):
            try:
                batch.append(next(self._records))
            except StopIteration:
                break
        return batch

    async def next_batch(self) -> list[InpxRecord | InpxRecordRejection]:
        return await asyncio.wrap_future(self._executor.submit(self._next_batch))

    def _close(self) -> None:
        if self._records is not None:
            self._records.close()

    async def _run_close(self) -> None:
        try:
            await asyncio.wrap_future(self._executor.submit(self._close))
        finally:
            self._executor.shutdown(wait=True)

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._run_close())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
        await self._close_task
        if cancelled:
            raise asyncio.CancelledError


@dataclass(slots=True)
class _MutableIds:
    archive: int
    author: int
    genre: int
    series: int
    book: int
    book_author: int
    book_genre: int

    @classmethod
    def from_counters(cls, counters: IdCounters) -> _MutableIds:
        return cls(**{name: getattr(counters, name) for name in cls.__dataclass_fields__})

    def next(self, name: str) -> int:
        current = getattr(self, name)
        if not isinstance(current, int):
            raise TypeError("ID counter is not an integer")
        value = current + 1
        setattr(self, name, value)
        return value


class CatalogImportService:
    """Build a hidden generation in bounded batches, then swap visibility atomically."""

    def __init__(
        self,
        repository: CatalogRepository,
        source_path: Path,
        archive_root: Path,
        *,
        namespace: str = "default",
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self._repository = repository
        self._source_path = source_path
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self._archive_root = archive_root
        self._namespace = namespace
        self._batch_size = batch_size

    async def import_source(
        self, trigger: ImportTrigger, fingerprint: SourceFingerprint
    ) -> ImportResult:
        counters = [0, 0, 0, 0]
        started = perf_counter()
        try:
            run_id, generation_id = await self._setup_import(trigger, fingerprint, counters)
        except asyncio.CancelledError:
            _log_import_terminal(
                trigger,
                ImportOutcome.INTERRUPTED,
                started,
                counters,
                failure_type="CancelledError",
            )
            raise
        except Exception as error:
            _LOGGER.exception(
                f"Catalog import setup failed phase=import failure_type={type(error).__name__}"
            )
            _log_import_terminal(
                trigger,
                ImportOutcome.FAILED,
                started,
                counters,
                failure_type=type(error).__name__,
            )
            raise
        _LOGGER.info(
            f"Catalog import started run_id={run_id} generation_id={generation_id} "
            f"trigger={trigger.value}"
        )
        staging_started = perf_counter()
        worker: _ParserWorker | None = None
        activation_committed = False
        terminal_outcome: ImportOutcome | None = None
        terminal_failure_type: str | None = None
        next_progress_record = _PROGRESS_RECORD_INTERVAL
        try:
            ids = _MutableIds.from_counters(await self._repository.id_counters())
            archives: dict[str, int] = {}
            authors: dict[tuple[str, str], int] = {}
            genres: dict[str, int] = {}
            series_entries: dict[tuple[str, str], int] = {}
            worker = _ParserWorker(self._source_path, self._batch_size)
            while batch := await worker.next_batch():
                counters[0] += len(batch)
                records: list[InpxRecord] = []
                for item in batch:
                    if isinstance(item, InpxRecordRejection):
                        rejection = item
                    else:
                        try:
                            _validate_mapped_metadata(item)
                        except CatalogDataError as error:
                            rejection = InpxRecordRejection(
                                str(error),
                                item.source_entry or "unknown",
                                item.line_number or 0,
                            )
                        else:
                            records.append(item)
                            continue
                    counters[3] += 1
                    _log_record_rejection(run_id, counters[3], rejection)
                counters[2] += sum(record.deleted for record in records)
                if records:
                    counters[1] += await self._write_records(
                        generation_id,
                        records,
                        ids,
                        archives,
                        authors,
                        genres,
                        series_entries,
                    )
                await self._repository.update_run_counters(run_id, _counter_tuple(counters))
                if counters[0] >= next_progress_record:
                    _LOGGER.info(
                        f"Catalog import progress run_id={run_id} read={counters[0]} "
                        f"imported={counters[1]} deleted={counters[2]} "
                        f"rejected={counters[3]}"
                    )
                    next_progress_record = (
                        counters[0] // _PROGRESS_RECORD_INTERVAL + 1
                    ) * _PROGRESS_RECORD_INTERVAL
            if counters[3]:
                _LOGGER.warning(
                    f"Catalog import encountered rejected records phase=staging run_id={run_id} "
                    f"generation_id={generation_id} rejected={counters[3]} "
                    f"details_logged={min(counters[3], _MAX_REJECTION_DETAIL_LOGS)}"
                )
            _validate_import_counters(counters)
            valid_records = counters[1] + counters[2]
            await self._repository.synchronize_explicit_id_sequences()
            staging_duration_ms = int((perf_counter() - staging_started) * 1000)
            _LOGGER.info(
                f"Catalog import staging completed phase=staging run_id={run_id} "
                f"generation_id={generation_id} duration_ms={staging_duration_ms} "
                f"read={counters[0]} imported={counters[1]} deleted={counters[2]} "
                f"rejected={counters[3]}"
            )

            materialization_started = perf_counter()
            _LOGGER.info(
                f"Catalog import materialization started phase=materialization run_id={run_id} "
                f"generation_id={generation_id} records={valid_records}"
            )
            await self._repository.materialize_generation_summaries(generation_id)
            materialization_duration_ms = int((perf_counter() - materialization_started) * 1000)
            _LOGGER.info(
                f"Catalog import materialization completed phase=materialization run_id={run_id} "
                f"generation_id={generation_id} duration_ms={materialization_duration_ms}"
            )

            validation_started = perf_counter()
            _LOGGER.info(
                f"Catalog import validation started phase=validation run_id={run_id} "
                f"generation_id={generation_id} records={valid_records}"
            )
            await self._repository.validate_generation_counts(generation_id, valid_records)
            validation_duration_ms = int((perf_counter() - validation_started) * 1000)
            _LOGGER.info(
                f"Catalog import validation completed phase=validation run_id={run_id} "
                f"generation_id={generation_id} duration_ms={validation_duration_ms}"
            )

            verification_started = perf_counter()
            _LOGGER.info(
                f"Catalog source verification started phase=source_verification run_id={run_id} "
                f"generation_id={generation_id}"
            )
            final_fingerprint = await hash_source(self._source_path, fingerprint)
            if final_fingerprint.sha256 != fingerprint.sha256:
                raise SourceChangedError("INPX source content changed while it was being imported")
            verification_duration_ms = int((perf_counter() - verification_started) * 1000)
            _LOGGER.info(
                f"Catalog source verification completed phase=source_verification run_id={run_id} "
                f"generation_id={generation_id} duration_ms={verification_duration_ms}"
            )

            activation_started = perf_counter()
            _LOGGER.info(
                f"Catalog activation started phase=activation run_id={run_id} "
                f"generation_id={generation_id}"
            )
            activation_task = asyncio.create_task(
                self._repository.activate(
                    run_id, generation_id, final_fingerprint, _counter_tuple(counters)
                )
            )
            try:
                await asyncio.shield(activation_task)
            except asyncio.CancelledError:
                try:
                    await asyncio.shield(activation_task)
                except Exception:
                    raise
                else:
                    activation_committed = True
                raise
            activation_committed = True
            activation_duration_ms = int((perf_counter() - activation_started) * 1000)
            _LOGGER.info(
                f"Catalog import activated phase=activation run_id={run_id} "
                f"generation_id={generation_id} duration_ms={activation_duration_ms} "
                f"read={counters[0]} imported={counters[1]} deleted={counters[2]} "
                f"rejected={counters[3]}"
            )
            try:
                status = await self._repository.latest_status()
            except Exception:
                _LOGGER.exception("Could not read status after catalog activation")
                status = None
            terminal_outcome = ImportOutcome.IMPORTED
            return ImportResult(ImportOutcome.IMPORTED, status)
        except asyncio.CancelledError:
            if not activation_committed:
                terminal_outcome = ImportOutcome.INTERRUPTED
                terminal_failure_type = "CancelledError"
                await self._finish_failed(
                    run_id,
                    generation_id,
                    ImportState.INTERRUPTED,
                    "Import interrupted by application shutdown",
                    counters,
                )
            else:
                terminal_outcome = ImportOutcome.IMPORTED
            raise
        except Exception as error:
            if activation_committed:
                _LOGGER.exception(
                    f"Catalog import follow-up failed after activation phase=import_follow_up "
                    f"failure_type={type(error).__name__}"
                )
                terminal_outcome = ImportOutcome.IMPORTED
                return ImportResult(ImportOutcome.IMPORTED, None)
            terminal_outcome = ImportOutcome.FAILED
            terminal_failure_type = type(error).__name__
            _LOGGER.exception(
                f"Catalog import failed phase=import failure_type={terminal_failure_type}"
            )
            await self._finish_failed(
                run_id,
                generation_id,
                ImportState.FAILED,
                _safe_summary(error),
                counters,
            )
            status = await self._repository.latest_status()
            return ImportResult(ImportOutcome.FAILED, status)
        finally:
            try:
                if worker is not None:
                    try:
                        await worker.close()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _LOGGER.exception("Could not close parser worker")
            finally:
                if terminal_outcome is not None:
                    _log_import_terminal(
                        trigger,
                        terminal_outcome,
                        started,
                        counters,
                        run_id=run_id,
                        generation_id=generation_id,
                        failure_type=terminal_failure_type,
                    )

    async def _setup_import(
        self,
        trigger: ImportTrigger,
        fingerprint: SourceFingerprint,
        counters: list[int],
    ) -> tuple[int, int]:
        setup_task = asyncio.create_task(self._repository.create_import(trigger, fingerprint))
        try:
            return await asyncio.shield(setup_task)
        except asyncio.CancelledError:
            try:
                run_id, generation_id = await asyncio.shield(setup_task)
            except Exception:
                _LOGGER.exception("Catalog import setup failed while cancellation was pending")
            else:
                await self._finish_failed(
                    run_id,
                    generation_id,
                    ImportState.INTERRUPTED,
                    "Import interrupted by application shutdown",
                    counters,
                )
            raise

    async def _finish_failed(
        self,
        run_id: int,
        generation_id: int | None,
        state: ImportState,
        error_summary: str,
        counters: list[int],
    ) -> None:
        finalization_task = asyncio.create_task(
            self._repository.finish_failed(
                run_id,
                generation_id,
                state,
                error_summary,
                _counter_tuple(counters),
            )
        )
        try:
            await asyncio.shield(finalization_task)
        except asyncio.CancelledError:
            while not finalization_task.done():
                try:
                    await asyncio.shield(finalization_task)
                except asyncio.CancelledError:
                    continue
            if not finalization_task.cancelled() and (
                finalization_error := finalization_task.exception()
            ):
                _LOGGER.error(
                    "Import failure finalization failed while cancellation was pending",
                    exc_info=finalization_error,
                )
            raise

    async def _write_records(
        self,
        generation_id: int,
        records: list[InpxRecord],
        ids: _MutableIds,
        archive_map: dict[str, int],
        author_map: dict[tuple[str, str], int],
        genre_map: dict[str, int],
        series_map: dict[tuple[str, str], int],
    ) -> int:
        archive_rows: list[ArchiveRow] = []
        author_rows: list[AuthorRow] = []
        genre_rows: list[GenreRow] = []
        series_rows: list[SeriesRow] = []
        book_rows: list[BookRow] = []
        book_author_rows: list[BookAuthorRow] = []
        book_genre_rows: list[BookGenreRow] = []
        search_rows: list[BookSearchRow] = []
        new_archive_paths = {
            record.locator.archive_relative_path.as_posix()
            for record in records
            if record.locator.archive_relative_path.as_posix() not in archive_map
        }
        availability = await asyncio.to_thread(
            archive_availability, self._archive_root, new_archive_paths
        )
        for record in records:
            archive_path = record.locator.archive_relative_path.as_posix()
            archive_id = archive_map.get(archive_path)
            if archive_id is None:
                archive_id = ids.next("archive")
                archive_map[archive_path] = archive_id
                archive_rows.append(
                    ArchiveRow(
                        id=archive_id,
                        generation_id=generation_id,
                        relative_path=archive_path,
                        available=availability[archive_path],
                    )
                )
            author_ids: list[int] = []
            seen_authors: set[int] = set()
            for name in record.authors:
                key = (normalize_sort_key(name), name)
                author_id = author_map.get(key)
                if author_id is None:
                    author_id = ids.next("author")
                    author_map[key] = author_id
                    author_rows.append(
                        AuthorRow(
                            id=author_id,
                            generation_id=generation_id,
                            name=name,
                            name_sort=key[0],
                        )
                    )
                if author_id not in seen_authors:
                    seen_authors.add(author_id)
                    author_ids.append(author_id)
            genre_ids: list[int] = []
            genre_labels: list[str] = []
            for code in dict.fromkeys(record.genres):
                label = genre_label(code)
                genre_id = genre_map.get(code)
                if genre_id is None:
                    genre_id = ids.next("genre")
                    genre_map[code] = genre_id
                    genre_rows.append(
                        GenreRow(
                            id=genre_id,
                            generation_id=generation_id,
                            code=code,
                            label=label,
                            label_sort=normalize_sort_key(label),
                        )
                    )
                genre_ids.append(genre_id)
                genre_labels.append(label)
            series_id = None
            if record.series is not None:
                series_key = (normalize_sort_key(record.series), record.series)
                series_id = series_map.get(series_key)
                if series_id is None:
                    series_id = ids.next("series")
                    series_map[series_key] = series_id
                    series_rows.append(
                        SeriesRow(
                            id=series_id,
                            generation_id=generation_id,
                            name=record.series,
                            name_sort=series_key[0],
                        )
                    )
            published_date = _parse_date(record.date)
            book_id = ids.next("book")
            public_id = derive_public_id(
                self._namespace, archive_path, record.locator.member_filename
            )
            book_rows.append(
                BookRow(
                    id=book_id,
                    generation_id=generation_id,
                    public_id=public_id,
                    archive_id=archive_id,
                    member_filename=record.locator.member_filename,
                    title=record.title,
                    title_sort=normalize_sort_key(record.title),
                    series_id=series_id,
                    series_number=record.series_number,
                    size=record.size,
                    libid=record.library_id,
                    published_date=published_date,
                    language=record.language,
                    original_format=record.extension,
                    rating=record.library_rating,
                    keywords=record.keywords,
                    hidden=record.deleted,
                )
            )
            for position, author_id in enumerate(author_ids):
                book_author_rows.append(
                    BookAuthorRow(
                        id=ids.next("book_author"),
                        book_id=book_id,
                        author_id=author_id,
                        position=position,
                    )
                )
            book_genre_rows.extend(
                BookGenreRow(id=ids.next("book_genre"), book_id=book_id, genre_id=genre_id)
                for genre_id in genre_ids
            )
            search_rows.append(
                BookSearchRow(
                    book_id=book_id,
                    generation_id=generation_id,
                    title=_search_projection(record.title),
                    authors=_search_projection(" ".join(record.authors)),
                    series=_search_projection(record.series or ""),
                    genres=_search_projection(" ".join(genre_labels)),
                    language=_search_projection(record.language or ""),
                )
            )
        await self._repository.write_batch(
            CatalogWriteBatch(
                archives=tuple(archive_rows),
                authors=tuple(author_rows),
                genres=tuple(genre_rows),
                series=tuple(series_rows),
                books=tuple(book_rows),
                book_authors=tuple(book_author_rows),
                book_genres=tuple(book_genre_rows),
                search_rows=tuple(search_rows),
            )
        )
        return sum(not record.deleted for record in records)


def normalize_sort_key(value: str) -> str:
    return normalize_text(value)


def _search_projection(value: str) -> str:
    return bound_search_projection(normalize_search_projection(value))


def _validate_mapped_metadata(record: InpxRecord) -> None:
    searchable_values = (
        record.title,
        *record.authors,
        *record.genres,
        *(
            value
            for value in (
                record.series,
                record.series_number,
                record.library_id,
                record.language,
                record.keywords,
            )
            if value is not None
        ),
    )
    if any("\x00" in value for value in searchable_values):
        raise CatalogDataError("A catalog record contains an unsupported NUL character")

    _validate_max_length(record.title, 1_024)
    _validate_max_length(normalize_sort_key(record.title), 1_024)
    for author in record.authors:
        _validate_max_length(author, 512)
        _validate_max_length(normalize_sort_key(author), 512)
    for genre in record.genres:
        _validate_max_length(genre, 128)
        _validate_max_length(normalize_sort_key(genre), 256)
    if record.series is not None:
        _validate_max_length(record.series, 512)
        _validate_max_length(normalize_sort_key(record.series), 512)
    if record.series_number is not None:
        _validate_max_length(record.series_number, 128)
    if record.keywords is not None:
        _validate_max_length(record.keywords, 2_048)
    if record.library_id is not None:
        _validate_max_length(record.library_id, 128)
    if record.language is not None:
        _validate_max_length(record.language, 32)
    _validate_max_length(record.extension, 32)
    if record.size > _MAX_BIGINT:
        raise CatalogDataError("A catalog record contains a size larger than supported")
    _parse_date(record.date)


def _validate_max_length(value: str, maximum: int) -> None:
    if len(value) > maximum:
        raise CatalogDataError("A catalog record contains metadata longer than the supported limit")


def _counter_tuple(counters: list[int]) -> tuple[int, int, int, int]:
    return counters[0], counters[1], counters[2], counters[3]


def derive_public_id(namespace: str, archive_path: str, member_filename: str) -> str:
    digest = hashlib.blake2b(digest_size=16)
    for value in (namespace, archive_path, member_filename):
        encoded = value.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")


def _parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise CatalogDataError("A catalog record contains an invalid publication date") from error


def _safe_summary(error: Exception) -> str:
    if isinstance(error, InpxParserError):
        return str(error)[:500]
    if isinstance(error, SourceChangedError | CatalogDataError):
        return str(error)[:500]
    if isinstance(error, PostgresError | BaseORMException):
        return "Catalog database rejected imported data"
    if isinstance(error, OSError):
        return "Could not read the configured catalog source"
    return f"Catalog import failed ({type(error).__name__})"[:500]
