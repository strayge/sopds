"""Catalog persistence through Tortoise models and explicit FTS boundaries."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TypedDict, cast

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.expressions import Q, Subquery
from tortoise.functions import Count, Max, Sum
from tortoise.models import Model
from tortoise.queryset import QuerySet
from tortoise.transactions import in_transaction

from sopds.acquisition.contracts import AcquisitionTarget
from sopds.catalog.contracts import (
    AuthorBookCounts,
    BookAvailability,
    CatalogBook,
    CatalogFilters,
    CatalogSnapshot,
    CatalogStatistics,
    FilterOption,
    SearchField,
)
from sopds.db.configuration import CONNECTION_NAME
from sopds.db.models import (
    Archive,
    ArchiveGenre,
    ArchiveLanguage,
    ArchiveOriginalFormat,
    Author,
    Book,
    BookAuthor,
    BookGenre,
    CatalogGeneration,
    CatalogSource,
    CatalogState,
    GenerationState,
    Genre,
    ImportRun,
    Series,
)
from sopds.db.rows import CatalogWriteBatch
from sopds.imports.fingerprint import SourceFingerprint
from sopds.imports.status import (
    ArchiveAvailabilitySummary,
    GenerationCleanupSummary,
    ImportState,
    ImportStatus,
    ImportTrigger,
    RecoverySummary,
)

DEFAULT_BATCH_SIZE = 2_000
PUBLIC_ID_LOOKUP_BATCH_SIZE = 500
_SEARCH_VECTORS = {
    SearchField.ALL: "all_vector",
    SearchField.TITLE: "title_vector",
    SearchField.AUTHOR: "authors_vector",
    SearchField.SERIES: "series_vector",
}
_EXPLICIT_ID_TABLES = (
    "archive",
    "author",
    "genre",
    "series",
    "book",
    "book_author",
    "book_genre",
)


@dataclass(frozen=True, slots=True)
class IdCounters:
    archive: int
    author: int
    genre: int
    series: int
    book: int
    book_author: int
    book_genre: int


class _BookValueRow(TypedDict):
    id: int
    public_id: str
    title: str
    series_id: int | None
    series_name: str | None
    series_number: str | None
    language: str | None
    original_format: str
    size: int
    member_filename: str
    published_date: date | None
    libid: str | None
    rating: int | None
    keywords: str | None
    hidden: bool
    archive_available: bool


async def _bulk_create_batched[ModelT: Model](
    model: type[ModelT], models: Sequence[ModelT], connection: BaseDBAsyncClient
) -> None:
    await model.bulk_create(models, batch_size=DEFAULT_BATCH_SIZE, using_db=connection)


class CatalogRepository:
    """Keep all persistence on the connection supplied by the database lifecycle."""

    def __init__(self, connection: BaseDBAsyncClient, *, cleanup_batch_size: int = 2_000) -> None:
        self._connection = connection
        self._cleanup_batch_size = cleanup_batch_size

    async def check_readiness(self) -> None:
        if not await CatalogState.filter(id=1).using_db(self._connection).exists():
            raise RuntimeError("Catalog database is not ready")

    async def ensure_source(self, namespace: str, path: Path) -> None:
        source = await CatalogSource.filter(id=1).using_db(self._connection).first()
        path_value = str(path)
        if source is None:
            await CatalogSource.create(
                using_db=self._connection,
                id=1,
                namespace=namespace,
                path=path_value,
            )
            return
        if source.namespace != namespace or source.path != path_value:
            source.fingerprint_size = None
            source.fingerprint_mtime_ns = None
            source.fingerprint_sha256 = None
        source.namespace = namespace
        source.path = path_value
        await source.save(using_db=self._connection)

    async def successful_fingerprint(self) -> SourceFingerprint | None:
        source = await CatalogSource.filter(id=1).using_db(self._connection).first()
        if source is None or source.fingerprint_sha256 is None:
            return None
        if source.fingerprint_size is None or source.fingerprint_mtime_ns is None:
            return None
        return SourceFingerprint(
            source.fingerprint_size,
            source.fingerprint_mtime_ns,
            source.fingerprint_sha256,
        )

    async def update_fingerprint_metadata(self, fingerprint: SourceFingerprint) -> None:
        await (
            CatalogSource.filter(id=1)
            .using_db(self._connection)
            .update(
                fingerprint_size=fingerprint.size,
                fingerprint_mtime_ns=fingerprint.mtime_ns,
                updated_at=datetime.now(UTC),
            )
        )

    async def create_run(
        self, trigger: ImportTrigger, fingerprint: SourceFingerprint | None
    ) -> int:
        run = await ImportRun.create(
            using_db=self._connection,
            trigger=trigger,
            state=ImportState.RUNNING,
            attempted_size=fingerprint.size if fingerprint is not None else None,
            attempted_mtime_ns=fingerprint.mtime_ns if fingerprint is not None else None,
            attempted_sha256=fingerprint.sha256 if fingerprint is not None else None,
        )
        return int(run.id)

    async def create_import(
        self, trigger: ImportTrigger, fingerprint: SourceFingerprint
    ) -> tuple[int, int]:
        async with in_transaction(CONNECTION_NAME) as transaction:
            run = await ImportRun.create(
                using_db=transaction,
                trigger=trigger,
                state=ImportState.RUNNING,
                attempted_size=fingerprint.size,
                attempted_mtime_ns=fingerprint.mtime_ns,
                attempted_sha256=fingerprint.sha256,
            )
            generation = await CatalogGeneration.create(
                using_db=transaction,
                state=GenerationState.IMPORTING,
            )
            changed = (
                await ImportRun.filter(id=run.id, state=ImportState.RUNNING)
                .using_db(transaction)
                .update(staging_generation_id=generation.id)
            )
            if changed != 1:
                raise RuntimeError("Import run could not be associated with its generation")
        return run.id, generation.id

    async def id_counters(self) -> IdCounters:
        models = (Archive, Author, Genre, Series, Book, BookAuthor, BookGenre)
        maxima: list[int] = []
        for model in models:
            values = (
                await model.all()
                .using_db(self._connection)
                .annotate(max_id=Max("id"))
                .values("max_id")
            )
            maxima.append(int(values[0]["max_id"] or 0) if values else 0)
        return IdCounters(
            archive=maxima[0],
            author=maxima[1],
            genre=maxima[2],
            series=maxima[3],
            book=maxima[4],
            book_author=maxima[5],
            book_genre=maxima[6],
        )

    async def write_batch(self, batch: CatalogWriteBatch) -> None:
        """Persist one bounded relational and search-projection batch atomically."""
        async with in_transaction(CONNECTION_NAME) as transaction:
            for offset in range(0, len(batch.archives), DEFAULT_BATCH_SIZE):
                archives = [
                    Archive(
                        id=row.id,
                        generation_id=row.generation_id,
                        relative_path=row.relative_path,
                        available=row.available,
                    )
                    for row in batch.archives[offset : offset + DEFAULT_BATCH_SIZE]
                ]
                await _bulk_create_batched(Archive, archives, transaction)
            for offset in range(0, len(batch.authors), DEFAULT_BATCH_SIZE):
                authors = [
                    Author(
                        id=row.id,
                        generation_id=row.generation_id,
                        name=row.name,
                        name_sort=row.name_sort,
                    )
                    for row in batch.authors[offset : offset + DEFAULT_BATCH_SIZE]
                ]
                await _bulk_create_batched(Author, authors, transaction)
            for offset in range(0, len(batch.genres), DEFAULT_BATCH_SIZE):
                genres = [
                    Genre(
                        id=row.id,
                        generation_id=row.generation_id,
                        code=row.code,
                        label=row.label,
                        label_sort=row.label_sort,
                    )
                    for row in batch.genres[offset : offset + DEFAULT_BATCH_SIZE]
                ]
                await _bulk_create_batched(Genre, genres, transaction)
            for offset in range(0, len(batch.series), DEFAULT_BATCH_SIZE):
                series = [
                    Series(
                        id=row.id,
                        generation_id=row.generation_id,
                        name=row.name,
                        name_sort=row.name_sort,
                    )
                    for row in batch.series[offset : offset + DEFAULT_BATCH_SIZE]
                ]
                await _bulk_create_batched(Series, series, transaction)
            for offset in range(0, len(batch.books), DEFAULT_BATCH_SIZE):
                books = [
                    Book(
                        id=row.id,
                        generation_id=row.generation_id,
                        public_id=row.public_id,
                        archive_id=row.archive_id,
                        member_filename=row.member_filename,
                        title=row.title,
                        title_sort=row.title_sort,
                        series_id=row.series_id,
                        series_number=row.series_number,
                        size=row.size,
                        libid=row.libid,
                        published_date=row.published_date,
                        language=row.language,
                        original_format=row.original_format,
                        rating=row.rating,
                        keywords=row.keywords,
                        hidden=row.hidden,
                    )
                    for row in batch.books[offset : offset + DEFAULT_BATCH_SIZE]
                ]
                await _bulk_create_batched(Book, books, transaction)
            for offset in range(0, len(batch.book_authors), DEFAULT_BATCH_SIZE):
                book_authors = [
                    BookAuthor(
                        id=row.id,
                        book_id=row.book_id,
                        author_id=row.author_id,
                        position=row.position,
                    )
                    for row in batch.book_authors[offset : offset + DEFAULT_BATCH_SIZE]
                ]
                await _bulk_create_batched(BookAuthor, book_authors, transaction)
            for offset in range(0, len(batch.book_genres), DEFAULT_BATCH_SIZE):
                book_genres = [
                    BookGenre(id=row.id, book_id=row.book_id, genre_id=row.genre_id)
                    for row in batch.book_genres[offset : offset + DEFAULT_BATCH_SIZE]
                ]
                await _bulk_create_batched(BookGenre, book_genres, transaction)
            if batch.search_rows:
                await transaction.execute_many(
                    "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
                    "VALUES ($1,$2,$3,$4,$5,$6,$7)",
                    [row.fts_parameters() for row in batch.search_rows],
                )

    async def synchronize_explicit_id_sequences(self) -> None:
        """Prevent later generated IDs colliding without updating a sequence per imported row."""
        async with in_transaction(CONNECTION_NAME) as transaction:
            for table in _EXPLICIT_ID_TABLES:
                await transaction.execute_query(
                    f"SELECT setval("  # noqa: S608
                    f"pg_get_serial_sequence('{table}', 'id')::regclass, "
                    f"COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table}"
                )

    async def update_run_counters(self, run_id: int, counters: tuple[int, int, int, int]) -> None:
        read, imported, deleted, rejected = counters
        await (
            ImportRun.filter(id=run_id, state=ImportState.RUNNING)
            .using_db(self._connection)
            .update(
                records_read=read,
                records_imported=imported,
                records_deleted=deleted,
                records_rejected=rejected,
            )
        )

    async def finish_failed(
        self,
        run_id: int,
        generation_id: int | None,
        state: ImportState,
        error_summary: str,
        counters: tuple[int, int, int, int],
    ) -> None:
        read, imported, deleted, rejected = counters
        now = datetime.now(UTC)
        async with in_transaction(CONNECTION_NAME) as transaction:
            run_changed = (
                await ImportRun.filter(id=run_id, state=ImportState.RUNNING)
                .using_db(transaction)
                .update(
                    state=state,
                    finished_at=now,
                    records_read=read,
                    records_imported=imported,
                    records_deleted=deleted,
                    records_rejected=rejected,
                    error_summary=error_summary,
                )
            )
            generation_changed = 0
            if generation_id is not None:
                generation_changed = (
                    await CatalogGeneration.filter(
                        id=generation_id, state=GenerationState.IMPORTING
                    )
                    .using_db(transaction)
                    .update(
                        state=GenerationState.FAILED,
                        completed_at=now,
                    )
                )
            if generation_id is not None and run_changed != generation_changed:
                raise RuntimeError("Import failure transition changed inconsistent state")

    async def activate(
        self,
        run_id: int,
        generation_id: int,
        fingerprint: SourceFingerprint,
        counters: tuple[int, int, int, int],
    ) -> None:
        read, imported, deleted, rejected = counters
        now = datetime.now(UTC)
        async with in_transaction(CONNECTION_NAME) as transaction:
            state = await self._locked_catalog_state(transaction)
            previous_value = state.active_generation_id  # type: ignore[attr-defined]
            previous = int(previous_value) if previous_value is not None else None
            previous_revision = state.updated_at
            if not isinstance(previous_revision, datetime):
                raise RuntimeError("Catalog state revision is invalid")
            if previous_revision.tzinfo is None:
                previous_revision = previous_revision.replace(tzinfo=UTC)
            revision = max(now, previous_revision.astimezone(UTC) + timedelta(microseconds=1))

            changed = (
                await CatalogGeneration.filter(id=generation_id, state=GenerationState.IMPORTING)
                .using_db(transaction)
                .update(
                    state=GenerationState.ACTIVE,
                    completed_at=now,
                    activated_at=now,
                )
            )
            if changed != 1:
                raise RuntimeError("Generation is not importing")
            changed = (
                await ImportRun.filter(
                    id=run_id,
                    state=ImportState.RUNNING,
                    staging_generation_id=generation_id,
                )
                .using_db(transaction)
                .update(
                    state=ImportState.SUCCEEDED,
                    finished_at=now,
                    records_read=read,
                    records_imported=imported,
                    records_deleted=deleted,
                    records_rejected=rejected,
                    error_summary=None,
                )
            )
            if changed != 1:
                raise RuntimeError("Import run is not running for this generation")
            if previous is not None:
                if previous == generation_id:
                    raise RuntimeError("Importing generation is already catalog-visible")
                changed = (
                    await CatalogGeneration.filter(id=previous, state=GenerationState.ACTIVE)
                    .using_db(transaction)
                    .update(state=GenerationState.SUPERSEDED)
                )
                if changed != 1:
                    raise RuntimeError("Previous catalog generation is not active")
            changed = (
                await CatalogState.filter(id=1)
                .using_db(transaction)
                .update(active_generation_id=generation_id, updated_at=revision)
            )
            if changed != 1:
                raise RuntimeError("Catalog state singleton could not be updated")
            changed = (
                await CatalogSource.filter(id=1)
                .using_db(transaction)
                .update(
                    fingerprint_size=fingerprint.size,
                    fingerprint_mtime_ns=fingerprint.mtime_ns,
                    fingerprint_sha256=fingerprint.sha256,
                    updated_at=now,
                )
            )
            if changed != 1:
                raise RuntimeError("Catalog source singleton is missing")

    async def materialize_generation_summaries(self, generation_id: int) -> None:
        """Refresh the bounded read projections before a generation can be activated."""
        async with in_transaction(CONNECTION_NAME) as transaction:
            await transaction.execute_query(
                """
                UPDATE catalog_generation
                SET visible_book_count = (
                        SELECT COUNT(*) FROM book
                        WHERE book.generation_id = catalog_generation.id AND NOT book.hidden
                    ),
                    hidden_book_count = (
                        SELECT COUNT(*) FROM book
                        WHERE book.generation_id = catalog_generation.id AND book.hidden
                    )
                WHERE id=$1
                """,
                [generation_id],
            )
            await transaction.execute_query(
                """
                UPDATE archive
                SET visible_book_count = (
                    SELECT COUNT(*) FROM book
                    WHERE book.archive_id = archive.id AND NOT book.hidden
                )
                WHERE generation_id=$1
                """,
                [generation_id],
            )
            await transaction.execute_query(
                "DELETE FROM archive_language WHERE archive_id IN "
                "(SELECT id FROM archive WHERE generation_id=$1)",
                [generation_id],
            )
            await transaction.execute_query(
                "DELETE FROM archive_original_format WHERE archive_id IN "
                "(SELECT id FROM archive WHERE generation_id=$1)",
                [generation_id],
            )
            await transaction.execute_query(
                "DELETE FROM archive_genre WHERE archive_id IN "
                "(SELECT id FROM archive WHERE generation_id=$1)",
                [generation_id],
            )
            await transaction.execute_query(
                """
                INSERT INTO archive_language(archive_id, language)
                SELECT DISTINCT b.archive_id, b.language
                FROM book b JOIN archive a ON a.id=b.archive_id
                WHERE b.generation_id=$1 AND a.generation_id=$2 AND NOT b.hidden
                  AND b.language IS NOT NULL
                  AND (b.series_id IS NULL OR EXISTS (SELECT 1 FROM series s
                      WHERE s.id=b.series_id AND s.generation_id=b.generation_id))
                """,
                [generation_id, generation_id],
            )
            await transaction.execute_query(
                """
                INSERT INTO archive_original_format(archive_id, original_format)
                SELECT DISTINCT b.archive_id, b.original_format
                FROM book b JOIN archive a ON a.id=b.archive_id
                WHERE b.generation_id=$1 AND a.generation_id=$2 AND NOT b.hidden
                  AND (b.series_id IS NULL OR EXISTS (SELECT 1 FROM series s
                      WHERE s.id=b.series_id AND s.generation_id=b.generation_id))
                """,
                [generation_id, generation_id],
            )
            await transaction.execute_query(
                """
                INSERT INTO archive_genre(archive_id, genre_id)
                SELECT DISTINCT b.archive_id, bg.genre_id
                FROM book b
                JOIN archive a ON a.id=b.archive_id
                JOIN book_genre bg ON bg.book_id=b.id
                JOIN genre g ON g.id=bg.genre_id
                WHERE b.generation_id=$1 AND a.generation_id=$2 AND NOT b.hidden
                  AND g.generation_id=b.generation_id
                  AND (b.series_id IS NULL OR EXISTS (SELECT 1 FROM series s
                      WHERE s.id=b.series_id AND s.generation_id=b.generation_id))
                """,
                [generation_id, generation_id],
            )

    async def validate_generation_counts(self, generation_id: int, expected: int) -> None:
        books = await Book.filter(generation_id=generation_id).using_db(self._connection).count()
        _, rows = await self._connection.execute_query(
            "SELECT COUNT(*) AS count FROM book_fts WHERE generation_id=$1", [generation_id]
        )
        fts = int(rows[0]["count"]) if len(rows) == 1 else -1
        generation_rows = await (
            CatalogGeneration.filter(id=generation_id)
            .using_db(self._connection)
            .values("visible_book_count", "hidden_book_count")
        )
        summary_complete = len(generation_rows) == 1
        if summary_complete:
            summary = generation_rows[0]
            visible = int(summary["visible_book_count"])
            hidden = int(summary["hidden_book_count"])
            visible_books = await (
                Book.filter(generation_id=generation_id, hidden=False)
                .using_db(self._connection)
                .count()
            )
            archive_count_rows = await (
                Archive.filter(generation_id=generation_id)
                .using_db(self._connection)
                .annotate(count=Sum("visible_book_count"))
                .values("count")
            )
            archive_book_count = archive_count_rows[0]["count"] if archive_count_rows else None
            summary_complete = (
                visible == visible_books
                and hidden == books - visible
                and int(archive_book_count or 0) == visible
            )
        if summary_complete:
            _, archive_rows = await self._connection.execute_query(
                """
                SELECT COUNT(*) AS count
                FROM archive a
                WHERE a.generation_id=$1 AND a.visible_book_count != (
                    SELECT COUNT(*) FROM book b
                    WHERE b.archive_id=a.id AND NOT b.hidden
                )
                """,
                [generation_id],
            )
            summary_complete = int(archive_rows[0]["count"]) == 0
        mapping_queries = (
            """
            WITH expected(archive_id, value) AS (
                SELECT DISTINCT b.archive_id, b.language FROM book b
                JOIN archive a ON a.id=b.archive_id
                WHERE b.generation_id=$1 AND a.generation_id=$2 AND NOT b.hidden
                  AND b.language IS NOT NULL
                  AND (b.series_id IS NULL OR EXISTS (SELECT 1 FROM series s
                      WHERE s.id=b.series_id AND s.generation_id=b.generation_id))
            ), actual(archive_id, value) AS (
                SELECT archive_id, language FROM archive_language al
                JOIN archive a ON a.id=al.archive_id WHERE a.generation_id=$3
            ), missing AS (
                SELECT archive_id, value FROM expected EXCEPT SELECT archive_id, value FROM actual
            ), extra AS (
                SELECT archive_id, value FROM actual EXCEPT SELECT archive_id, value FROM expected
            ), differences AS (
                SELECT archive_id, value FROM missing UNION ALL SELECT archive_id, value FROM extra
            ) SELECT COUNT(*) AS count FROM differences
            """,
            """
            WITH expected(archive_id, value) AS (
                SELECT DISTINCT b.archive_id, b.original_format FROM book b
                JOIN archive a ON a.id=b.archive_id
                WHERE b.generation_id=$1 AND a.generation_id=$2 AND NOT b.hidden
                  AND (b.series_id IS NULL OR EXISTS (SELECT 1 FROM series s
                      WHERE s.id=b.series_id AND s.generation_id=b.generation_id))
            ), actual(archive_id, value) AS (
                SELECT archive_id, original_format FROM archive_original_format af
                JOIN archive a ON a.id=af.archive_id WHERE a.generation_id=$3
            ), missing AS (
                SELECT archive_id, value FROM expected EXCEPT SELECT archive_id, value FROM actual
            ), extra AS (
                SELECT archive_id, value FROM actual EXCEPT SELECT archive_id, value FROM expected
            ), differences AS (
                SELECT archive_id, value FROM missing UNION ALL SELECT archive_id, value FROM extra
            ) SELECT COUNT(*) AS count FROM differences
            """,
            """
            WITH expected(archive_id, value) AS (
                SELECT DISTINCT b.archive_id, bg.genre_id FROM book b
                JOIN archive a ON a.id=b.archive_id
                JOIN book_genre bg ON bg.book_id=b.id
                JOIN genre g ON g.id=bg.genre_id
                WHERE b.generation_id=$1 AND a.generation_id=$2 AND NOT b.hidden
                  AND g.generation_id=b.generation_id
                  AND (b.series_id IS NULL OR EXISTS (SELECT 1 FROM series s
                      WHERE s.id=b.series_id AND s.generation_id=b.generation_id))
            ), actual(archive_id, value) AS (
                SELECT archive_id, genre_id FROM archive_genre ag
                JOIN archive a ON a.id=ag.archive_id WHERE a.generation_id=$3
            ), missing AS (
                SELECT archive_id, value FROM expected EXCEPT SELECT archive_id, value FROM actual
            ), extra AS (
                SELECT archive_id, value FROM actual EXCEPT SELECT archive_id, value FROM expected
            ), differences AS (
                SELECT archive_id, value FROM missing UNION ALL SELECT archive_id, value FROM extra
            ) SELECT COUNT(*) AS count FROM differences
            """,
        )
        if summary_complete:
            for query in mapping_queries:
                _, mapping_rows = await self._connection.execute_query(
                    query, [generation_id, generation_id, generation_id]
                )
                if not mapping_rows or int(mapping_rows[0]["count"]) != 0:
                    summary_complete = False
                    break
        if books != expected or fts != expected or not summary_complete:
            raise RuntimeError("Persisted catalog projection counts do not match imported records")

    async def latest_status(self) -> ImportStatus | None:
        run = await ImportRun.all().using_db(self._connection).order_by("-id").first()
        if run is None:
            return None
        fingerprint = None
        if run.attempted_size is not None and run.attempted_mtime_ns is not None:
            fingerprint = SourceFingerprint(
                run.attempted_size,
                run.attempted_mtime_ns,
                run.attempted_sha256,
            )
        return ImportStatus(
            run_id=run.id,
            trigger=run.trigger,
            state=run.state,
            started_at=run.started_at,
            finished_at=run.finished_at,
            attempted_fingerprint=fingerprint,
            records_read=run.records_read,
            records_imported=run.records_imported,
            records_deleted=run.records_deleted,
            records_rejected=run.records_rejected,
            error_summary=run.error_summary,
            generation_id=await self._run_generation_id(run.id),
        )

    async def _run_generation_id(self, run_id: int) -> int | None:
        values = (
            await ImportRun.filter(id=run_id)
            .using_db(self._connection)
            .values("staging_generation_id")
        )
        value = values[0]["staging_generation_id"]
        return int(value) if value is not None else None

    async def recover(self) -> RecoverySummary:
        now = datetime.now(UTC)
        interrupted_runs = await (
            ImportRun.filter(state=ImportState.RUNNING)
            .using_db(self._connection)
            .update(
                state=ImportState.INTERRUPTED,
                finished_at=now,
                error_summary="Import interrupted by process shutdown",
            )
        )
        failed_generations = await (
            CatalogGeneration.filter(state=GenerationState.IMPORTING)
            .using_db(self._connection)
            .update(state=GenerationState.FAILED, completed_at=now)
        )
        cleanup = await self.cleanup_inactive()
        return RecoverySummary(
            interrupted_runs=interrupted_runs,
            failed_generations=failed_generations,
            removed_generations=cleanup.removed_generations,
        )

    async def cleanup_inactive(self) -> GenerationCleanupSummary:
        removed_generations = 0
        while generation_id := await self._next_inactive_generation():
            eligible = True
            for model in (None, Book, Archive, Author, Genre, Series):
                while True:
                    if model is None:
                        deleted = await self._delete_fts_batch(generation_id)
                    else:
                        deleted = await self._delete_generation_batch(model, generation_id)
                    if deleted is None:
                        eligible = False
                        break
                    if deleted == 0:
                        break
                if not eligible:
                    break
            if eligible and await self._delete_inactive_generation(generation_id):
                removed_generations += 1
        return GenerationCleanupSummary(removed_generations=removed_generations)

    async def _next_inactive_generation(self) -> int | None:
        async with in_transaction(CONNECTION_NAME) as transaction:
            active_id = await self._locked_active_generation_id(transaction)
            stale_query = CatalogGeneration.filter(
                state__in=(GenerationState.SUPERSEDED, GenerationState.FAILED)
            ).using_db(transaction)
            if active_id is not None:
                stale_query = stale_query.exclude(id=active_id)
            stale = await stale_query.order_by("id").first()
            return stale.id if stale is not None else None

    async def _locked_catalog_state(self, transaction: BaseDBAsyncClient) -> CatalogState:
        state = await CatalogState.filter(id=1).using_db(transaction).select_for_update().first()
        if state is None:
            raise RuntimeError("Catalog state singleton is missing")
        return state

    async def _locked_active_generation_id(self, transaction: BaseDBAsyncClient) -> int | None:
        state = await self._locked_catalog_state(transaction)
        active_value = state.active_generation_id  # type: ignore[attr-defined]
        return int(active_value) if active_value is not None else None

    async def _is_inactive_cleanup_target(
        self, transaction: BaseDBAsyncClient, generation_id: int
    ) -> bool:
        active_id = await self._locked_active_generation_id(transaction)
        if active_id == generation_id:
            return False
        values = (
            await CatalogGeneration.filter(id=generation_id).using_db(transaction).values("state")
        )
        return bool(
            values and values[0]["state"] in (GenerationState.SUPERSEDED, GenerationState.FAILED)
        )

    async def _delete_fts_batch(self, generation_id: int) -> int | None:
        """Commit one guarded projection batch so cancellation preserves bounded progress."""
        async with in_transaction(CONNECTION_NAME) as transaction:
            if not await self._is_inactive_cleanup_target(transaction, generation_id):
                return None
            _, rows = await transaction.execute_query(
                "WITH doomed AS ("
                "SELECT book_id FROM book_fts WHERE generation_id=$1 "
                "ORDER BY book_id LIMIT $2"
                "), deleted AS ("
                "DELETE FROM book_fts AS f USING doomed "
                "WHERE f.book_id=doomed.book_id AND f.generation_id=$1 "
                "RETURNING f.book_id"
                ") SELECT COUNT(*) AS count FROM deleted",
                [generation_id, self._cleanup_batch_size],
            )
            if len(rows) != 1:
                raise RuntimeError("Inactive search projection cleanup did not return a count")
            return int(rows[0]["count"])

    async def _delete_generation_batch(
        self,
        model: type[Archive] | type[Author] | type[Genre] | type[Series] | type[Book],
        generation_id: int,
    ) -> int | None:
        async with in_transaction(CONNECTION_NAME) as transaction:
            if not await self._is_inactive_cleanup_target(transaction, generation_id):
                return None
            ids = (
                await model.filter(generation_id=generation_id)
                .using_db(transaction)
                .order_by("id")
                .limit(self._cleanup_batch_size)
                .values_list("id", flat=True)
            )
            if not ids:
                return 0
            await model.filter(id__in=ids).using_db(transaction).delete()
            return len(ids)

    async def _delete_inactive_generation(self, generation_id: int) -> bool:
        async with in_transaction(CONNECTION_NAME) as transaction:
            if not await self._is_inactive_cleanup_target(transaction, generation_id):
                return False
            deleted = (
                await CatalogGeneration.filter(id=generation_id).using_db(transaction).delete()
            )
            if deleted != 1:
                raise RuntimeError("Inactive catalog generation could not be removed")
            return True

    async def active_archives(self) -> list[tuple[int, str]]:
        state_values = (
            await CatalogState.filter(id=1)
            .using_db(self._connection)
            .values("active_generation_id")
        )
        active_value = state_values[0]["active_generation_id"] if state_values else None
        if active_value is None:
            return []
        rows = (
            await Archive.filter(generation_id=int(active_value))
            .using_db(self._connection)
            .values_list("id", "relative_path")
        )
        return [(archive_id, relative_path) for archive_id, relative_path in rows]

    async def update_archive_availability(
        self, values: dict[int, bool]
    ) -> ArchiveAvailabilitySummary:
        if not values:
            return ArchiveAvailabilitySummary(0, 0, 0, 0, 0)
        async with in_transaction(CONNECTION_NAME) as transaction:
            state = await self._locked_catalog_state(transaction)
            active_value = state.active_generation_id  # type: ignore[attr-defined]
            if active_value is None:
                return ArchiveAvailabilitySummary(0, 0, 0, 0, 0)
            active_generation_id = int(active_value)
            changed_archives: list[Archive] = []
            checked = available_count = unavailable_count = 0
            changed_to_available = changed_to_unavailable = 0
            archive_ids = list(values)
            for offset in range(0, len(archive_ids), self._cleanup_batch_size):
                chunk_ids = archive_ids[offset : offset + self._cleanup_batch_size]
                archives = await Archive.filter(
                    id__in=chunk_ids,
                    generation_id=active_generation_id,
                ).using_db(transaction)
                for archive in archives:
                    available = values[int(archive.id)]
                    checked += 1
                    if available:
                        available_count += 1
                    else:
                        unavailable_count += 1
                    if archive.available != available:
                        archive.available = available
                        changed_archives.append(archive)
                        if available:
                            changed_to_available += 1
                        else:
                            changed_to_unavailable += 1
            summary = ArchiveAvailabilitySummary(
                checked,
                available_count,
                unavailable_count,
                changed_to_available,
                changed_to_unavailable,
            )
            if not changed_archives:
                return summary
            await Archive.bulk_update(
                changed_archives,
                fields=("available",),
                batch_size=self._cleanup_batch_size,
                using_db=transaction,
            )
            previous = state.updated_at
            if not isinstance(previous, datetime):
                raise RuntimeError("Catalog state revision is invalid")
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            revision = max(now, previous.astimezone(UTC) + timedelta(microseconds=1))
            await CatalogState.filter(id=1).using_db(transaction).update(updated_at=revision)
            return summary

    async def active_snapshot(self) -> CatalogSnapshot:
        rows = await (
            CatalogState.filter(id=1)
            .using_db(self._connection)
            .limit(1)
            .values("active_generation_id", "updated_at")
        )
        if not rows or rows[0]["active_generation_id"] is None:
            return CatalogSnapshot(None, datetime(1970, 1, 1, tzinfo=UTC))
        updated = rows[0]["updated_at"]
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        return CatalogSnapshot(int(rows[0]["active_generation_id"]), updated.astimezone(UTC))

    async def catalog_statistics(self, generation_id: int | None) -> CatalogStatistics:
        _, size_rows = await self._connection.execute_query(
            "SELECT pg_database_size(current_database()) AS database_size_bytes"
        )
        database_size_bytes = int(size_rows[0]["database_size_bytes"])
        if generation_id is None:
            return CatalogStatistics(
                total_books=0,
                hidden_books=0,
                missed_books=0,
                active_books=0,
                generation_activated_at=None,
                database_size_bytes=database_size_bytes,
            )

        generation_rows = await (
            CatalogGeneration.filter(id=generation_id)
            .using_db(self._connection)
            .values("activated_at", "visible_book_count", "hidden_book_count")
        )
        activated_at = generation_rows[0]["activated_at"] if generation_rows else None
        if activated_at is not None:
            if activated_at.tzinfo is None:
                activated_at = activated_at.replace(tzinfo=UTC)
            activated_at = activated_at.astimezone(UTC)
        if not generation_rows:
            visible_books = 0
            persisted_hidden_books = 0
        else:
            visible_books = int(generation_rows[0]["visible_book_count"])
            persisted_hidden_books = int(generation_rows[0]["hidden_book_count"])
        missed_rows = await (
            Archive.filter(generation_id=generation_id, available=False)
            .using_db(self._connection)
            .annotate(count=Sum("visible_book_count"))
            .values("count")
        )
        missed_books = int(missed_rows[0]["count"] or 0) if missed_rows else 0
        run_rows = await (
            ImportRun.filter(
                staging_generation_id=generation_id,
                state=ImportState.SUCCEEDED,
            )
            .using_db(self._connection)
            .order_by("-id")
            .limit(1)
            .values("records_deleted")
        )
        recorded_hidden_books = int(run_rows[0]["records_deleted"]) if run_rows else 0
        # Existing generations predate persisted hidden metadata. Keep their statistics
        # accurate until the next forced import populates searchable hidden rows.
        hidden_books = max(persisted_hidden_books, recorded_hidden_books)
        total_books = visible_books + hidden_books
        return CatalogStatistics(
            total_books=total_books,
            hidden_books=hidden_books,
            missed_books=missed_books,
            active_books=total_books - hidden_books - missed_books,
            generation_activated_at=activated_at,
            database_size_bytes=database_size_bytes,
        )

    async def vacuum(self) -> None:
        """Use top-level execution because PostgreSQL rejects VACUUM inside transactions."""
        await self._connection.execute_query("VACUUM (ANALYZE)")

    @staticmethod
    def _acquisition_target(
        row: tuple[int, object, object, int, object, object, object],
    ) -> AcquisitionTarget:
        generation_id, public_id, title, size, original_format, archive_path, member = row
        return AcquisitionTarget(
            generation_id=int(generation_id),
            public_id=str(public_id),
            title=str(title),
            expected_size=int(size),
            original_format=str(original_format),
            archive_relative_path=str(archive_path),
            member_filename=str(member),
        )

    async def acquisition_targets(
        self,
        public_ids: Sequence[str],
        *,
        expected_generation_id: int | None = None,
    ) -> dict[str, AcquisitionTarget]:
        """Keep bulk target lookups below the database parameter limit."""
        targets: dict[str, AcquisitionTarget] = {}
        for offset in range(0, len(public_ids), PUBLIC_ID_LOOKUP_BATCH_SIZE):
            chunk = public_ids[offset : offset + PUBLIC_ID_LOOKUP_BATCH_SIZE]
            query = Book.filter(
                public_id__in=chunk,
                archive__available=True,
                generation__active_catalog_states__id=1,
            )
            if expected_generation_id is not None:
                query = query.filter(generation_id=expected_generation_id)
            rows = await query.using_db(self._connection).values_list(
                "generation_id",
                "public_id",
                "title",
                "size",
                "original_format",
                "archive__relative_path",
                "member_filename",
            )
            for row in rows:
                target = self._acquisition_target(row)
                targets[target.public_id] = target
        return targets

    def _visible_books(
        self,
        generation_id: int,
        *,
        language: str | None,
        genre: str | None,
        original_format: str | None,
        author: str | None,
        series: str | None,
        without_series: bool = False,
        include_missed: bool = False,
        include_hidden: bool = False,
    ) -> QuerySet[Book]:
        visibility = Q(hidden=False, archive__available=True)
        if include_missed:
            visibility |= Q(hidden=False, archive__available=False)
        if include_hidden:
            visibility |= Q(hidden=True)
        query = Book.filter(
            Q(series_id=None) | Q(series__generation_id=generation_id),
            visibility,
            generation_id=generation_id,
            archive__generation_id=generation_id,
        ).using_db(self._connection)
        if language is not None:
            query = query.filter(language=language)
        if genre is not None:
            query = query.filter(
                genre_links__genre__generation_id=generation_id,
                genre_links__genre__code=genre,
            )
        if original_format is not None:
            query = query.filter(original_format=original_format)
        if author is not None:
            query = query.filter(
                author_links__author__generation_id=generation_id,
                author_links__author__name=author,
            )
        if series is not None:
            query = query.filter(series__generation_id=generation_id, series__name=series)
        if without_series:
            query = query.filter(series_id=None)
        return query

    async def browse_book_ids(
        self,
        generation_id: int,
        *,
        language: str | None,
        genre: str | None,
        original_format: str | None,
        author: str | None,
        series: str | None,
        without_series: bool = False,
        include_missed: bool = False,
        include_hidden: bool = False,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[tuple[int, str, str]]:
        query = self._visible_books(
            generation_id,
            language=language,
            genre=genre,
            original_format=original_format,
            author=author,
            series=series,
            without_series=without_series,
            include_missed=include_missed,
            include_hidden=include_hidden,
        )
        if after is not None:
            title_sort, public_id = after
            query = query.filter(
                Q(title_sort__gt=title_sort) | Q(title_sort=title_sort, public_id__gt=public_id)
            )
        rows = await (
            query.distinct()
            .order_by("title_sort", "public_id")
            .limit(limit)
            .values_list("id", "title_sort", "public_id")
        )
        return [
            (int(book_id), str(title_sort), str(public_id))
            for book_id, title_sort, public_id in rows
        ]

    async def search_book_ids(
        self,
        generation_id: int,
        tokens: tuple[str, ...],
        *,
        search_field: SearchField,
        language: str | None,
        genre: str | None,
        original_format: str | None,
        author: str | None,
        series: str | None,
        without_series: bool = False,
        include_missed: bool = False,
        include_hidden: bool = False,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[tuple[int, str, str]]:
        vector = _SEARCH_VECTORS[search_field]
        sql = (
            "SELECT DISTINCT b.id,b.title_sort,b.public_id FROM book_fts bf "  # noqa: S608
            "JOIN book b ON b.id=bf.book_id "
            "JOIN archive a ON a.id=b.archive_id "
            f"WHERE bf.{vector} @@ plainto_tsquery('simple'::regconfig, $1) "
            "AND b.generation_id=$2 AND bf.generation_id=$2 AND a.generation_id=$2 "
            "AND ((NOT b.hidden AND a.available) "
            "OR ($3 AND NOT b.hidden AND NOT a.available) OR ($4 AND b.hidden)) "
            "AND (b.series_id IS NULL OR EXISTS "
            "(SELECT 1 FROM series bs WHERE bs.id=b.series_id AND bs.generation_id=$2)) "
            "AND ($5::text IS NULL OR b.language=$5) "
            "AND ($6::text IS NULL OR b.original_format=$6) "
            "AND ($7::text IS NULL OR EXISTS ("
            "SELECT 1 FROM book_genre bg JOIN genre g ON g.id=bg.genre_id "
            "WHERE bg.book_id=b.id AND g.generation_id=$2 AND g.code=$7)) "
            "AND ($8::text IS NULL OR EXISTS ("
            "SELECT 1 FROM book_author ba JOIN author au ON au.id=ba.author_id "
            "WHERE ba.book_id=b.id AND au.generation_id=$2 AND au.name=$8)) "
            "AND ($9::text IS NULL OR EXISTS ("
            "SELECT 1 FROM series s WHERE s.id=b.series_id "
            "AND s.generation_id=$2 AND s.name=$9)) "
            "AND (NOT $10::boolean OR b.series_id IS NULL) "
            "AND ($11::text IS NULL OR b.title_sort>$11 "
            "OR (b.title_sort=$12 AND b.public_id>$13)) "
            "ORDER BY b.title_sort,b.public_id LIMIT $14"
        )
        parameters: list[object] = [
            " ".join(tokens),
            generation_id,
            include_missed,
            include_hidden,
            language,
            original_format,
            genre,
            author,
            series,
            without_series,
            after[0] if after is not None else None,
            after[0] if after is not None else None,
            after[1] if after is not None else None,
            limit,
        ]
        _, rows = await self._connection.execute_query(sql, parameters)
        return [(int(row["id"]), str(row["title_sort"]), str(row["public_id"])) for row in rows]

    async def summaries(
        self,
        generation_id: int,
        book_ids: list[int],
        *,
        include_missed: bool = False,
        include_hidden: bool = False,
    ) -> list[CatalogBook]:
        if not book_ids:
            return []
        by_id = await self._catalog_books(
            self._visible_books(
                generation_id,
                language=None,
                genre=None,
                original_format=None,
                author=None,
                series=None,
                include_missed=include_missed,
                include_hidden=include_hidden,
            ).filter(id__in=book_ids),
            generation_id,
        )
        return [by_id[book_id] for book_id in book_ids if book_id in by_id]

    async def summaries_by_public_ids(
        self,
        generation_id: int,
        public_ids: Sequence[str],
    ) -> list[CatalogBook]:
        if not public_ids:
            return []

        by_public_id: dict[str, CatalogBook] = {}
        for offset in range(0, len(public_ids), PUBLIC_ID_LOOKUP_BATCH_SIZE):
            chunk = public_ids[offset : offset + PUBLIC_ID_LOOKUP_BATCH_SIZE]
            books = await self._catalog_books(
                Book.filter(
                    Q(series_id=None) | Q(series__generation_id=generation_id),
                    generation_id=generation_id,
                    archive__generation_id=generation_id,
                    public_id__in=chunk,
                ).using_db(self._connection),
                generation_id,
            )
            by_public_id.update((book.public_id, book) for book in books.values())
        return [by_public_id[public_id] for public_id in public_ids if public_id in by_public_id]

    async def detail(
        self,
        generation_id: int,
        public_id: str,
        *,
        include_missed: bool = False,
        include_hidden: bool = False,
    ) -> CatalogBook | None:
        books = await self._catalog_books(
            self._visible_books(
                generation_id,
                language=None,
                genre=None,
                original_format=None,
                author=None,
                series=None,
                include_missed=include_missed,
                include_hidden=include_hidden,
            ).filter(public_id=public_id),
            generation_id,
        )
        if not books:
            return None
        return next(iter(books.values()))

    async def detail_by_id(self, generation_id: int, book_id: int) -> CatalogBook | None:
        books = await self._catalog_books(
            self._visible_books(
                generation_id,
                language=None,
                genre=None,
                original_format=None,
                author=None,
                series=None,
                include_missed=False,
                include_hidden=False,
            ).filter(id=book_id),
            generation_id,
        )
        return books.get(book_id)

    async def author_name_by_id(self, generation_id: int, author_id: int) -> str | None:
        author = (
            await Author.filter(id=author_id, generation_id=generation_id)
            .using_db(self._connection)
            .first()
        )
        return None if author is None else author.name

    async def series_name_by_id(self, generation_id: int, series_id: int) -> str | None:
        series = (
            await Series.filter(id=series_id, generation_id=generation_id)
            .using_db(self._connection)
            .first()
        )
        return None if series is None else series.name

    async def _catalog_books(
        self, query: QuerySet[Book], generation_id: int
    ) -> dict[int, CatalogBook]:
        """Project flat values so reverse-relation containers cannot retain model cycles."""
        rows = cast(
            list[_BookValueRow],
            await query.using_db(self._connection).values(
                "id",
                "public_id",
                "title",
                "series_id",
                "series_number",
                "language",
                "original_format",
                "size",
                "member_filename",
                "published_date",
                "libid",
                "rating",
                "keywords",
                "hidden",
                series_name="series__name",
                archive_available="archive__available",
            ),
        )
        if not rows:
            return {}

        book_ids = [row["id"] for row in rows]
        author_rows = cast(
            list[tuple[int, int, str]],
            await BookAuthor.filter(
                book_id__in=book_ids,
                book__generation_id=generation_id,
                author__generation_id=generation_id,
            )
            .using_db(self._connection)
            .order_by("book_id", "position")
            .values_list("book_id", "author_id", "author__name"),
        )
        genre_rows = cast(
            list[tuple[int, str, str]],
            await BookGenre.filter(
                book_id__in=book_ids,
                book__generation_id=generation_id,
                genre__generation_id=generation_id,
            )
            .using_db(self._connection)
            .values_list("book_id", "genre__code", "genre__label"),
        )

        authors_by_book: dict[int, list[tuple[int, str]]] = {}
        for book_id, author_id, name in author_rows:
            authors_by_book.setdefault(book_id, []).append((author_id, name))
        genres_by_book: dict[int, list[tuple[str, str]]] = {}
        for book_id, code, label in genre_rows:
            genres_by_book.setdefault(book_id, []).append((code, label))

        books: dict[int, CatalogBook] = {}
        for row in rows:
            book_id = row["id"]
            archive_available = row["archive_available"]
            authors = authors_by_book.get(book_id, ())
            books[book_id] = CatalogBook(
                public_id=row["public_id"],
                book_id=book_id,
                title=row["title"],
                authors=tuple(name for _author_id, name in authors),
                author_ids=tuple(author_id for author_id, _name in authors),
                series=row["series_name"],
                series_id=row["series_id"],
                series_number=row["series_number"],
                language=row["language"],
                original_format=row["original_format"],
                size=row["size"],
                member_filename=row["member_filename"],
                genres=tuple(
                    sorted(
                        genres_by_book.get(book_id, ()),
                        key=lambda item: (item[1].casefold(), item[0]),
                    )
                ),
                published_date=row["published_date"],
                libid=row["libid"],
                rating=row["rating"],
                keywords=row["keywords"],
                availability=self._availability(
                    hidden=row["hidden"], archive_available=archive_available
                ),
                downloadable=archive_available,
            )
        return books

    @staticmethod
    def _availability(*, hidden: bool, archive_available: bool) -> BookAvailability:
        if hidden:
            return BookAvailability.HIDDEN
        if not archive_available:
            return BookAvailability.MISSED
        return BookAvailability.ACTIVE

    def _available_genres(self, generation_id: int) -> QuerySet[Genre]:
        available_genre_ids = ArchiveGenre.filter(
            archive__generation_id=generation_id,
            archive__available=True,
        ).values("genre_id")
        return Genre.filter(
            generation_id=generation_id,
            id__in=Subquery(available_genre_ids),
        ).using_db(self._connection)

    def _available_authors(self, generation_id: int) -> QuerySet[Author]:
        return Author.filter(
            Q(book_links__book__series_id=None)
            | Q(book_links__book__series__generation_id=generation_id),
            generation_id=generation_id,
            book_links__book__generation_id=generation_id,
            book_links__book__archive__generation_id=generation_id,
            book_links__book__archive__available=True,
            book_links__book__hidden=False,
        ).using_db(self._connection)

    def _available_series(self, generation_id: int, author: str | None = None) -> QuerySet[Series]:
        query = Series.filter(
            generation_id=generation_id,
            books__generation_id=generation_id,
            books__archive__generation_id=generation_id,
            books__archive__available=True,
            books__hidden=False,
        ).using_db(self._connection)
        if author is not None:
            query = query.filter(
                books__author_links__author__generation_id=generation_id,
                books__author_links__author__name=author,
            )
        return query

    async def author_book_counts(self, generation_id: int, author: str) -> AuthorBookCounts:
        books = self._visible_books(
            generation_id,
            language=None,
            genre=None,
            original_format=None,
            author=author,
            series=None,
        )
        total = await books.distinct().count()
        without_series = await books.filter(series_id=None).distinct().count()
        series_ids = (
            await self._available_series(generation_id, author)
            .distinct()
            .values_list("id", flat=True)
        )
        return AuthorBookCounts(
            series=len(series_ids),
            without_series=without_series,
            total=total,
        )

    async def navigation_prefix_buckets(
        self,
        generation_id: int,
        kind: str,
        prefix: str,
        *,
        author: str | None = None,
    ) -> list[tuple[str, int]]:
        prefix_end = prefix + "\U0010ffff"
        parameters: list[object] = [generation_id, len(prefix) + 1, prefix, prefix_end]
        if kind == "authors":
            table = "author"
            sort_column = "name_sort"
            availability = (
                "EXISTS (SELECT 1 FROM book_author ba "
                "JOIN book b ON b.id=ba.book_id "
                "JOIN archive ar ON ar.id=b.archive_id "
                "WHERE ba.author_id=source.id AND b.generation_id=$1 "
                "AND ar.generation_id=$1 AND ar.available=TRUE AND b.hidden=FALSE)"
            )
        elif kind == "series":
            table = "series"
            sort_column = "name_sort"
            author_filter = ""
            if author is not None:
                parameters.append(author)
                author_filter = (
                    "AND EXISTS (SELECT 1 FROM book_author ba "
                    "JOIN author au ON au.id=ba.author_id "
                    "WHERE ba.book_id=b.id AND au.generation_id=$1 AND au.name=$5) "
                )
            availability = (
                "EXISTS (SELECT 1 FROM book b JOIN archive ar ON ar.id=b.archive_id "  # noqa: S608
                "WHERE b.series_id=source.id AND b.generation_id=$1 "
                "AND ar.generation_id=$1 AND ar.available=TRUE AND b.hidden=FALSE "
                f"{author_filter})"
            )
        elif kind == "titles":
            table = "book"
            sort_column = "title_sort"
            availability = (
                "EXISTS (SELECT 1 FROM archive ar WHERE ar.id=source.archive_id "
                "AND ar.generation_id=$1 AND ar.available=TRUE) AND source.hidden=FALSE"
            )
        else:
            raise ValueError("Invalid adaptive navigation kind")
        _, rows = await self._connection.execute_query(
            f"SELECT substring(source.{sort_column} FROM $2 FOR 1) AS next_character, "  # noqa: S608
            f"COUNT(DISTINCT source.id) AS item_count FROM {table} source "
            "WHERE source.generation_id=$1 AND "
            "($3='' OR (source."
            f"{sort_column} >= $3 AND source.{sort_column} < $4)) AND {availability} "
            "GROUP BY 1 ORDER BY 1",
            parameters,
        )
        return [
            (str(row["next_character"]), int(row["item_count"]))
            for row in rows
            if row["next_character"] is not None
        ]

    async def navigation_prefix_items(
        self,
        generation_id: int,
        kind: str,
        prefix: str,
        *,
        exact: bool,
        author: str | None,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[tuple[int, str, str, str, str, int | None]]:
        prefix_end = prefix + "\U0010ffff"
        if kind == "authors":
            author_query = self._available_authors(generation_id)
            if exact:
                author_query = author_query.filter(name_sort=prefix)
            elif prefix:
                author_query = author_query.filter(name_sort__gte=prefix, name_sort__lt=prefix_end)
            if after is not None:
                author_query = author_query.filter(
                    Q(name_sort__gt=after[0]) | Q(name_sort=after[0], id__gt=int(after[1]))
                )
            rows = await (
                author_query.annotate(book_count=Count("book_links__book__id", distinct=True))
                .distinct()
                .order_by("name_sort", "id")
                .limit(limit)
                .values_list("id", "name_sort", "name", "book_count")
            )
            return [
                (
                    int(row[0]),
                    str(row[1]),
                    str(row[0]),
                    str(row[2]),
                    str(row[2]),
                    int(row[3]),
                )
                for row in rows
            ]
        if kind == "series":
            series_query = self._available_series(generation_id, author)
            if exact:
                series_query = series_query.filter(name_sort=prefix)
            elif prefix:
                series_query = series_query.filter(name_sort__gte=prefix, name_sort__lt=prefix_end)
            if after is not None:
                series_query = series_query.filter(
                    Q(name_sort__gt=after[0]) | Q(name_sort=after[0], id__gt=int(after[1]))
                )
            rows = await (
                series_query.annotate(book_count=Count("books__id", distinct=True))
                .distinct()
                .order_by("name_sort", "id")
                .limit(limit)
                .values_list("id", "name_sort", "name", "book_count")
            )
            return [
                (
                    int(row[0]),
                    str(row[1]),
                    str(row[0]),
                    str(row[2]),
                    str(row[2]),
                    int(row[3]),
                )
                for row in rows
            ]
        if kind == "titles":
            title_query = self._visible_books(
                generation_id,
                language=None,
                genre=None,
                original_format=None,
                author=None,
                series=None,
            )
            if exact:
                title_query = title_query.filter(title_sort=prefix)
            elif prefix:
                title_query = title_query.filter(title_sort__gte=prefix, title_sort__lt=prefix_end)
            if after is not None:
                title_query = title_query.filter(
                    Q(title_sort__gt=after[0]) | Q(title_sort=after[0], public_id__gt=after[1])
                )
            rows = await (
                title_query.distinct()
                .order_by("title_sort", "public_id")
                .limit(limit)
                .values_list("id", "title_sort", "public_id", "title")
            )
            return [
                (int(row[0]), str(row[1]), str(row[2]), str(row[2]), str(row[3]), None)
                for row in rows
            ]
        raise ValueError("Invalid adaptive navigation kind")

    async def navigation_items(
        self,
        generation_id: int,
        kind: str,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[tuple[str, str, str, str]]:
        if kind == "authors":
            author_query = self._available_authors(generation_id)
            if after is not None:
                author_query = author_query.filter(
                    Q(name_sort__gt=after[0]) | Q(name_sort=after[0], id__gt=int(after[1]))
                )
            rows = await (
                author_query.distinct()
                .order_by("name_sort", "id")
                .limit(limit)
                .values_list("id", "name_sort", "name")
            )
            return [(str(row[0]), str(row[1]), str(row[2]), str(row[2])) for row in rows]
        if kind == "genres":
            genre_query = self._available_genres(generation_id)
            if after is not None:
                genre_query = genre_query.filter(
                    Q(label_sort__gt=after[0]) | Q(label_sort=after[0], id__gt=int(after[1]))
                )
            rows = await (
                genre_query.distinct()
                .order_by("label_sort", "id")
                .limit(limit)
                .values_list("id", "label_sort", "code", "label")
            )
            return [(str(row[0]), str(row[1]), str(row[2]), str(row[3])) for row in rows]
        if kind == "series":
            series_query = self._available_series(generation_id)
            if after is not None:
                series_query = series_query.filter(
                    Q(name_sort__gt=after[0]) | Q(name_sort=after[0], id__gt=int(after[1]))
                )
            rows = await (
                series_query.distinct()
                .order_by("name_sort", "id")
                .limit(limit)
                .values_list("id", "name_sort", "name")
            )
            return [(str(row[0]), str(row[1]), str(row[2]), str(row[2])) for row in rows]
        language_query = ArchiveLanguage.filter(
            archive__generation_id=generation_id,
            archive__available=True,
        ).using_db(self._connection)
        if after is not None:
            language_query = language_query.filter(language__gt=after[0])
        values = (
            await language_query.distinct()
            .order_by("language")
            .limit(limit)
            .values_list("language", flat=True)
        )
        return [(str(value), str(value), str(value), str(value)) for value in values]

    async def catalog_filters(self, generation_id: int) -> CatalogFilters:
        languages = await (
            ArchiveLanguage.filter(
                archive__generation_id=generation_id,
                archive__available=True,
            )
            .using_db(self._connection)
            .distinct()
            .order_by("language")
            .values_list("language", flat=True)
        )
        formats = await (
            ArchiveOriginalFormat.filter(
                archive__generation_id=generation_id,
                archive__available=True,
            )
            .using_db(self._connection)
            .distinct()
            .order_by("original_format")
            .values_list("original_format", flat=True)
        )
        genres = await (
            self._available_genres(generation_id)
            .distinct()
            .order_by("label_sort", "code")
            .values_list("code", "label", "label_sort")
        )
        return CatalogFilters(
            languages=tuple(
                FilterOption(value=str(value), label=str(value)) for value in languages
            ),
            genres=tuple(
                FilterOption(value=str(code), label=str(label)) for code, label, _sort in genres
            ),
            original_formats=tuple(
                FilterOption(value=str(value), label=str(value)) for value in formats
            ),
        )
