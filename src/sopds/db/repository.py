"""Catalog persistence through Tortoise models and explicit FTS boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tortoise.backends.base.client import BaseDBAsyncClient
from tortoise.functions import Max
from tortoise.transactions import in_transaction

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
    ImportState,
    ImportTrigger,
    Series,
)
from sopds.db.rows import CatalogWriteBatch
from sopds.imports.fingerprint import SourceFingerprint
from sopds.imports.status import ImportStatus

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

    async def recover(self) -> None:
        now = datetime.now(UTC)
        await (
            ImportRun.filter(state=ImportState.RUNNING)
            .using_db(self._connection)
            .update(
                state=ImportState.INTERRUPTED,
                finished_at=now,
                error_summary="Import interrupted by process shutdown",
            )
        )
        await (
            CatalogGeneration.filter(state=GenerationState.IMPORTING)
            .using_db(self._connection)
            .update(state=GenerationState.FAILED, completed_at=now)
        )
        await self.cleanup_inactive()

    async def cleanup_inactive(self) -> None:
        state_values = (
            await CatalogState.filter(id=1)
            .using_db(self._connection)
            .values("active_generation_id")
        )
        active_value = state_values[0]["active_generation_id"] if state_values else None
        active_id = int(active_value) if active_value is not None else None
        while True:
            stale_query = CatalogGeneration.filter(
                state__in=(GenerationState.SUPERSEDED, GenerationState.FAILED)
            ).using_db(self._connection)
            if active_id is not None:
                stale_query = stale_query.exclude(id=active_id)
            stale = await stale_query.order_by("id").first()
            if stale is None:
                return
            await self._delete_fts_rows(stale.id)
            await self._delete_generation_rows(Book, stale.id)
            for model in (Archive, Author, Genre, Series):
                await self._delete_generation_rows(model, stale.id)
            await CatalogGeneration.filter(id=stale.id).using_db(self._connection).delete()

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

    async def update_archive_availability(self, values: dict[int, bool]) -> None:
        archive_ids = list(values)
        for offset in range(0, len(archive_ids), self._cleanup_batch_size):
            chunk_ids = archive_ids[offset : offset + self._cleanup_batch_size]
            archives = await Archive.filter(id__in=chunk_ids).using_db(self._connection)
            for archive in archives:
                archive.available = values[archive.id]
            await (
                Archive.all()
                .using_db(self._connection)
                .bulk_update(
                    archives,
                    fields=("available",),
                    batch_size=self._cleanup_batch_size,
                )
            )
