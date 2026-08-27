"""Catalog persistence through Tortoise models and explicit FTS boundaries."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.expressions import Q, Subquery
from tortoise.functions import Max
from tortoise.query_utils import Prefetch
from tortoise.queryset import QuerySet
from tortoise.transactions import in_transaction

from sopds.acquisition.contracts import AcquisitionTarget
from sopds.catalog.contracts import (
    BookDetail,
    BookSummary,
    CatalogFilters,
    CatalogSnapshot,
    CatalogStatistics,
    FilterOption,
)
from sopds.db.configuration import CONNECTION_NAME
from sopds.db.models import (
    Archive,
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


@dataclass(frozen=True, slots=True)
class IdCounters:
    archive: int
    author: int
    genre: int
    series: int
    book: int
    book_author: int
    book_genre: int


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
        """Persist relational rows through bounded ORM batches and FTS through SQLite."""
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
                await Archive.bulk_create(
                    archives, batch_size=DEFAULT_BATCH_SIZE, using_db=transaction
                )
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
                await Author.bulk_create(
                    authors, batch_size=DEFAULT_BATCH_SIZE, using_db=transaction
                )
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
                await Genre.bulk_create(genres, batch_size=DEFAULT_BATCH_SIZE, using_db=transaction)
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
                await Series.bulk_create(
                    series, batch_size=DEFAULT_BATCH_SIZE, using_db=transaction
                )
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
                    )
                    for row in batch.books[offset : offset + DEFAULT_BATCH_SIZE]
                ]
                await Book.bulk_create(books, batch_size=DEFAULT_BATCH_SIZE, using_db=transaction)
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
                await BookAuthor.bulk_create(
                    book_authors, batch_size=DEFAULT_BATCH_SIZE, using_db=transaction
                )
            for offset in range(0, len(batch.book_genres), DEFAULT_BATCH_SIZE):
                book_genres = [
                    BookGenre(id=row.id, book_id=row.book_id, genre_id=row.genre_id)
                    for row in batch.book_genres[offset : offset + DEFAULT_BATCH_SIZE]
                ]
                await BookGenre.bulk_create(
                    book_genres, batch_size=DEFAULT_BATCH_SIZE, using_db=transaction
                )
            if batch.search_rows:
                await transaction.execute_many(
                    "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
                    "VALUES (?,?,?,?,?,?,?)",
                    [row.fts_parameters() for row in batch.search_rows],
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
            state_values = (
                await CatalogState.filter(id=1).using_db(transaction).values("active_generation_id")
            )
            if len(state_values) != 1:
                raise RuntimeError("Catalog state singleton is missing")
            previous_value = state_values[0]["active_generation_id"]
            previous = int(previous_value) if previous_value is not None else None
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
                .update(active_generation_id=generation_id, updated_at=now)
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

    async def validate_generation_counts(self, generation_id: int, expected: int) -> None:
        books = await Book.filter(generation_id=generation_id).using_db(self._connection).count()
        # FTS5 is intentionally not represented as an ORM model, so its count remains raw.
        _, rows = await self._connection.execute_query(
            "SELECT COUNT(*) AS count FROM book_fts WHERE generation_id=?", [generation_id]
        )
        fts = int(rows[0]["count"]) if len(rows) == 1 else -1
        if books != expected or fts != expected:
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
        state_values = (
            await CatalogState.filter(id=1)
            .using_db(self._connection)
            .values("active_generation_id")
        )
        active_value = state_values[0]["active_generation_id"] if state_values else None
        active_id = int(active_value) if active_value is not None else None
        removed_generations = 0
        while True:
            stale_query = CatalogGeneration.filter(
                state__in=(GenerationState.SUPERSEDED, GenerationState.FAILED)
            ).using_db(self._connection)
            if active_id is not None:
                stale_query = stale_query.exclude(id=active_id)
            stale = await stale_query.order_by("id").first()
            if stale is None:
                return GenerationCleanupSummary(removed_generations=removed_generations)
            await self._delete_fts_rows(stale.id)
            await self._delete_generation_rows(Book, stale.id)
            for model in (Archive, Author, Genre, Series):
                await self._delete_generation_rows(model, stale.id)
            await CatalogGeneration.filter(id=stale.id).using_db(self._connection).delete()
            removed_generations += 1

    async def _delete_fts_rows(self, generation_id: int) -> None:
        """Bound FTS deletion because SQLite FTS5 is outside Tortoise's model system."""
        while True:
            deleted, _ = await self._connection.execute_query(
                "DELETE FROM book_fts WHERE rowid IN "
                "(SELECT rowid FROM book_fts WHERE generation_id=? LIMIT ?)",
                [generation_id, self._cleanup_batch_size],
            )
            if deleted == 0:
                return

    async def _delete_generation_rows(
        self,
        model: type[Archive] | type[Author] | type[Genre] | type[Series] | type[Book],
        generation_id: int,
    ) -> None:
        while True:
            ids = (
                await model.filter(generation_id=generation_id)
                .using_db(self._connection)
                .limit(self._cleanup_batch_size)
                .values_list("id", flat=True)
            )
            if not ids:
                return
            await model.filter(id__in=ids).using_db(self._connection).delete()

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
            state = (
                await CatalogState.filter(id=1)
                .using_db(transaction)
                .select_for_update()
                .select_related("active_generation")
                .first()
            )
            if state is None or state.active_generation is None:
                return ArchiveAvailabilitySummary(0, 0, 0, 0, 0)
            active_generation_id = int(state.active_generation.id)
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
            if previous.tzinfo is None:
                previous = previous.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            revision = max(now, previous.astimezone(UTC) + timedelta(microseconds=1))
            await CatalogState.filter(id=1).using_db(transaction).update(updated_at=revision)
            return summary

    async def active_generation_id(self) -> int | None:
        values = await (
            CatalogState.filter(id=1).using_db(self._connection).values("active_generation_id")
        )
        value = values[0]["active_generation_id"] if values else None
        return int(value) if value is not None else None

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
            "SELECT "
            "(SELECT page_count FROM pragma_page_count) * "
            "(SELECT page_size FROM pragma_page_size) AS database_size_bytes"
        )
        database_size_bytes = int(size_rows[0]["database_size_bytes"])
        if generation_id is None:
            return CatalogStatistics(0, 0, None, database_size_bytes)

        generation_rows = await (
            CatalogGeneration.filter(id=generation_id)
            .using_db(self._connection)
            .values("activated_at")
        )
        activated_at = generation_rows[0]["activated_at"] if generation_rows else None
        if activated_at is not None:
            if activated_at.tzinfo is None:
                activated_at = activated_at.replace(tzinfo=UTC)
            activated_at = activated_at.astimezone(UTC)
        active_books = await (
            Book.filter(generation_id=generation_id).using_db(self._connection).count()
        )
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
        deleted_books = int(run_rows[0]["records_deleted"]) if run_rows else 0
        return CatalogStatistics(
            active_books,
            deleted_books,
            activated_at,
            database_size_bytes,
        )

    async def vacuum(self) -> None:
        await self._connection.execute_script("VACUUM")

    async def acquisition_target(self, public_id: str) -> AcquisitionTarget | None:
        """Materialize all file coordinates from one active-generation query."""
        rows = await (
            Book.filter(
                public_id=public_id,
                archive__available=True,
                generation__active_catalog_states__id=1,
            )
            .using_db(self._connection)
            .limit(1)
            .values_list(
                "generation_id",
                "public_id",
                "title",
                "size",
                "original_format",
                "archive__relative_path",
                "member_filename",
            )
        )
        if not rows:
            return None
        generation_id, row_public_id, title, size, original_format, archive_path, member = rows[0]
        return AcquisitionTarget(
            generation_id=int(generation_id),
            public_id=str(row_public_id),
            title=str(title),
            expected_size=int(size),
            original_format=str(original_format),
            archive_relative_path=str(archive_path),
            member_filename=str(member),
        )

    def _available_books(
        self,
        generation_id: int,
        *,
        language: str | None,
        genre: str | None,
        original_format: str | None,
        author: str | None,
        series: str | None,
    ) -> QuerySet[Book]:
        query = Book.filter(
            Q(series_id=None) | Q(series__generation_id=generation_id),
            generation_id=generation_id,
            archive__generation_id=generation_id,
            archive__available=True,
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
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[tuple[int, str, str]]:
        query = self._available_books(
            generation_id,
            language=language,
            genre=genre,
            original_format=original_format,
            author=author,
            series=series,
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
        match: str,
        *,
        language: str | None,
        genre: str | None,
        original_format: str | None,
        author: str | None,
        series: str | None,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[tuple[int, str, str]]:
        sql = (
            "SELECT b.id,b.title_sort,b.public_id FROM book_fts "
            "JOIN book b ON b.id=book_fts.book_id "
            "JOIN archive a ON a.id=b.archive_id "
            "WHERE book_fts MATCH ? AND b.generation_id=? "
            "AND book_fts.generation_id=? AND a.generation_id=? AND a.available=1 "
            "AND (b.series_id IS NULL OR EXISTS "
            "(SELECT 1 FROM series bs WHERE bs.id=b.series_id AND bs.generation_id=?))"
        )
        parameters: list[int | str] = [
            match,
            generation_id,
            generation_id,
            generation_id,
            generation_id,
        ]
        if language is not None:
            sql += " AND b.language=?"
            parameters.append(language)
        if original_format is not None:
            sql += " AND b.original_format=?"
            parameters.append(original_format)
        if genre is not None:
            sql += (
                " AND EXISTS (SELECT 1 FROM book_genre bg JOIN genre g ON g.id=bg.genre_id "
                "WHERE bg.book_id=b.id AND g.generation_id=? AND g.code=?)"
            )
            parameters.extend((generation_id, genre))
        if author is not None:
            sql += (
                " AND EXISTS (SELECT 1 FROM book_author ba JOIN author au ON au.id=ba.author_id "
                "WHERE ba.book_id=b.id AND au.generation_id=? AND au.name=?)"
            )
            parameters.extend((generation_id, author))
        if series is not None:
            sql += (
                " AND EXISTS (SELECT 1 FROM series s WHERE s.id=b.series_id "
                "AND s.generation_id=? AND s.name=?)"
            )
            parameters.extend((generation_id, series))
        if after is not None:
            sql += " AND (b.title_sort>? OR (b.title_sort=? AND b.public_id>?))"
            parameters.extend((after[0], after[0], after[1]))
        sql += " ORDER BY b.title_sort,b.public_id LIMIT ?"
        parameters.append(limit)
        _, rows = await self._connection.execute_query(sql, parameters)
        return [(int(row["id"]), str(row["title_sort"]), str(row["public_id"])) for row in rows]

    async def summaries(self, generation_id: int, book_ids: list[int]) -> list[BookSummary]:
        if not book_ids:
            return []
        state = await (
            CatalogState.filter(id=1, active_generation_id=generation_id)
            .using_db(self._connection)
            .values("updated_at")
        )
        if not state:
            return []
        updated_at = state[0]["updated_at"]
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        books = await self._hydrated_books(
            Book.filter(
                Q(series_id=None) | Q(series__generation_id=generation_id),
                id__in=book_ids,
                generation_id=generation_id,
                archive__generation_id=generation_id,
                archive__available=True,
            ),
            generation_id,
        )
        by_id = {int(book.id): self._summary(book, updated_at) for book in books}
        return [by_id[book_id] for book_id in book_ids if book_id in by_id]

    async def detail(self, generation_id: int, public_id: str) -> BookDetail | None:
        books = await self._hydrated_books(
            Book.filter(
                Q(series_id=None) | Q(series__generation_id=generation_id),
                generation_id=generation_id,
                public_id=public_id,
                archive__generation_id=generation_id,
                archive__available=True,
            ),
            generation_id,
        )
        if not books:
            return None
        book = books[0]
        return BookDetail(
            public_id=book.public_id,
            title=book.title,
            authors=tuple(link.author.name for link in book.author_links),
            genres=tuple(
                sorted(
                    ((link.genre.code, link.genre.label) for link in book.genre_links),
                    key=lambda item: (item[1].casefold(), item[0]),
                )
            ),
            series=book.series.name if book.series is not None else None,
            series_number=book.series_number,
            size=book.size,
            libid=book.libid,
            published_date=book.published_date,
            language=book.language,
            original_format=book.original_format,
            rating=book.rating,
            keywords=book.keywords,
        )

    async def _hydrated_books(self, query: QuerySet[Book], generation_id: int) -> list[Book]:
        return await (
            query.using_db(self._connection)
            .select_related("series")
            .prefetch_related(
                Prefetch(
                    "author_links",
                    queryset=BookAuthor.filter(
                        book__generation_id=generation_id,
                        author__generation_id=generation_id,
                    )
                    .order_by("position")
                    .select_related("author"),
                ),
                Prefetch(
                    "genre_links",
                    queryset=BookGenre.filter(
                        book__generation_id=generation_id,
                        genre__generation_id=generation_id,
                    ).select_related("genre"),
                ),
            )
        )

    @staticmethod
    def _summary(book: Book, updated_at: datetime) -> BookSummary:
        return BookSummary(
            public_id=book.public_id,
            title=book.title,
            authors=tuple(link.author.name for link in book.author_links),
            series=book.series.name if book.series is not None else None,
            series_number=book.series_number,
            language=book.language,
            original_format=book.original_format,
            size=book.size,
            genres=tuple(
                sorted(
                    ((link.genre.code, link.genre.label) for link in book.genre_links),
                    key=lambda item: (item[1].casefold(), item[0]),
                )
            ),
            published_date=book.published_date,
            libid=book.libid,
            rating=book.rating,
            keywords=book.keywords,
            updated_at=updated_at,
        )

    async def navigation_items(
        self,
        generation_id: int,
        kind: str,
        *,
        after: tuple[str, str] | None,
        limit: int,
    ) -> list[tuple[str, str, str, str]]:
        if kind == "authors":
            author_query = Author.filter(
                Q(book_links__book__series_id=None)
                | Q(book_links__book__series__generation_id=generation_id),
                generation_id=generation_id,
                book_links__book__generation_id=generation_id,
                book_links__book__archive__generation_id=generation_id,
                book_links__book__archive__available=True,
            ).using_db(self._connection)
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
            available_genre_ids = BookGenre.filter(
                Q(book__series_id=None) | Q(book__series__generation_id=generation_id),
                book__generation_id=generation_id,
                book__archive__generation_id=generation_id,
                book__archive__available=True,
                genre__generation_id=generation_id,
            ).values("genre_id")
            genre_query = Genre.filter(
                generation_id=generation_id,
                id__in=Subquery(available_genre_ids),
            ).using_db(self._connection)
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
            available_series_ids = (
                Book.filter(
                    generation_id=generation_id,
                    archive__generation_id=generation_id,
                    archive__available=True,
                    series__generation_id=generation_id,
                )
                .exclude(series_id=None)
                .values("series_id")
            )
            series_query = Series.filter(
                generation_id=generation_id,
                id__in=Subquery(available_series_ids),
            ).using_db(self._connection)
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
        language_query = (
            Book.filter(
                Q(series_id=None) | Q(series__generation_id=generation_id),
                generation_id=generation_id,
                archive__generation_id=generation_id,
                archive__available=True,
            )
            .using_db(self._connection)
            .exclude(language=None)
        )
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
        base = Book.filter(
            Q(series_id=None) | Q(series__generation_id=generation_id),
            generation_id=generation_id,
            archive__generation_id=generation_id,
            archive__available=True,
        ).using_db(self._connection)
        languages = sorted(
            str(value)
            for value in await base.exclude(language=None)
            .distinct()
            .values_list("language", flat=True)
        )
        formats = sorted(
            str(value) for value in await base.distinct().values_list("original_format", flat=True)
        )
        available_genre_ids = BookGenre.filter(
            Q(book__series_id=None) | Q(book__series__generation_id=generation_id),
            book__generation_id=generation_id,
            book__archive__generation_id=generation_id,
            book__archive__available=True,
            genre__generation_id=generation_id,
        ).values("genre_id")
        genre_rows = await (
            Genre.filter(
                generation_id=generation_id,
                id__in=Subquery(available_genre_ids),
            )
            .using_db(self._connection)
            .order_by("label_sort", "code")
            .values_list("code", "label")
        )
        return CatalogFilters(
            languages=tuple(FilterOption(value=value, label=value) for value in languages),
            genres=tuple(FilterOption(value=code, label=label) for code, label in genre_rows),
            original_formats=tuple(FilterOption(value=value, label=value) for value in formats),
        )
