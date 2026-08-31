"""Focused PostgreSQL repository and import parity coverage."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import asyncpg  # type: ignore[import-untyped]
import pytest

from sopds.catalog.contracts import CatalogRequest, SearchField
from sopds.catalog.search import (
    normalize_search_projection,
    normalize_search_text,
    normalize_text,
    query_tokens,
)
from sopds.catalog.service import CatalogService
from sopds.config import AppConfig
from sopds.db.connection import close_database, initialize_database
from sopds.db.migrations_runner import apply_migrations
from sopds.db.models import (
    Archive,
    Author,
    Book,
    BookAuthor,
    BookGenre,
    CatalogGeneration,
    CatalogState,
    GenerationState,
    Genre,
    ImportRun,
    Series,
)
from sopds.db.repository import CatalogRepository
from sopds.imports.coordinator import ImportCoordinator
from sopds.imports.fingerprint import SourceFingerprint
from sopds.imports.status import ImportOutcome, ImportState, ImportTrigger
from tests.conftest import reset_test_database

_FIELDS = (
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
_SEPARATOR = "\x04"


def _line(**overrides: str) -> bytes:
    values = {
        "AUTHOR": "José Álvarez:Иван Ёлкин:",
        "GENRE": "sf:",
        "TITLE": "Café résumé naïve Ёлка",
        "SERIES": "Série",
        "SERNO": "1",
        "FILE": "book",
        "SIZE": "123",
        "LIBID": "lib-1",
        "DEL": "0",
        "EXT": "fb2",
        "DATE": "2024-02-03",
        "LANG": "ru",
        "LIBRATE": "4",
        "KEYWORDS": "one,two",
    }
    values.update(overrides)
    return (_SEPARATOR.join(values[name] for name in _FIELDS) + _SEPARATOR).encode() + b"\r\n"


def _write_inpx(path: Path, *lines: bytes) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("nested/books.inp", b"".join(lines))


async def _reset_database(config: AppConfig) -> None:
    await reset_test_database(config.database)
    await apply_migrations(config.database)


async def _wait_for_catalog_state_lock_waiters(
    connection: asyncpg.Connection, expected: int
) -> set[int]:
    """Observe submitted transactions at the contested row lock before assertions proceed."""
    for _ in range(500):
        rows = await connection.fetch(
            "SELECT DISTINCT activity.pid FROM pg_stat_activity activity "
            "WHERE activity.datname=current_database() "
            "AND activity.pid<>pg_backend_pid() "
            "AND cardinality(pg_blocking_pids(activity.pid))>0 "
            "AND EXISTS (SELECT 1 FROM pg_locks locks "
            "JOIN pg_class relation ON relation.oid=locks.relation "
            "JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace "
            "WHERE locks.pid=activity.pid AND relation.relname='catalog_state' "
            "AND namespace.nspname='public')"
        )
        waiters = {int(row["pid"]) for row in rows}
        if len(waiters) == expected:
            return waiters
        await asyncio.sleep(0.01)
    activity = await connection.fetch(
        "SELECT pid, state, wait_event_type, wait_event, query FROM pg_stat_activity "
        "WHERE datname=current_database() AND pid<>pg_backend_pid() ORDER BY pid"
    )
    raise AssertionError(
        f"Expected {expected} catalog_state lock waiters, found {len(waiters)}: {activity!r}"
    )


@asynccontextmanager
async def _catalog(
    config: AppConfig, *, cleanup_batch_size: int = 1
) -> AsyncIterator[tuple[CatalogRepository, ImportCoordinator]]:
    await _reset_database(config)
    context = await initialize_database(config.database)
    repository = CatalogRepository(context.db(), cleanup_batch_size=cleanup_batch_size)
    coordinator = ImportCoordinator(
        repository,
        config.catalog.inpx_path,
        config.catalog.archive_root,
        batch_size=1,
    )
    await coordinator.recover()
    try:
        yield repository, coordinator
    finally:
        await close_database(context)


def test_search_normalization_folds_only_latin_diacritics() -> None:
    assert normalize_text("Café résumé naïve Ёлка Й") == "café résumé naïve елка й"
    assert normalize_search_text("Café résumé naïve Ёлка Й") == "cafe resume naive елка й"
    assert query_tokens("CAFÉ, résumé naïve Ёлка") == ("cafe", "resume", "naive", "елка")
    punctuated = "v1.2.3 J.R.R. reader@example.org books.example.org"
    assert normalize_search_projection(punctuated) == (
        "v1 2 3 j r r reader example org books example org"
    )
    assert query_tokens("J.R.R. v1.2.3") == ("j", "r", "r", "v1", "2", "3")


async def test_punctuation_projection_matches_title_author_and_series_queries(
    app_config: AppConfig,
) -> None:
    archive_path = app_config.catalog.archive_root / "nested" / "books.zip"
    archive_path.parent.mkdir()
    archive_path.touch()
    _write_inpx(
        app_config.catalog.inpx_path,
        _line(
            TITLE="Release v1.2.3",
            AUTHOR="J.R.R. Tolkien:reader@example.org:",
            SERIES="books.example.org",
        ),
    )

    async with _catalog(app_config) as (repository, coordinator):
        result = await coordinator._request(ImportTrigger.MANUAL, force=True)
        assert result.outcome is ImportOutcome.IMPORTED
        _, rows = await repository._connection.execute_query(
            "SELECT title,authors,series FROM book_fts"
        )
        assert dict(rows[0]) == {
            "title": "release v1 2 3",
            "authors": "j r r tolkien reader example org",
            "series": "books example org",
        }

        catalog = CatalogService(repository, b"postgresql-test-cursor-key")
        for query, field in (
            ("v1.2.3", SearchField.TITLE),
            ("J.R.R.", SearchField.AUTHOR),
            ("reader@example.org", SearchField.AUTHOR),
            ("books.example.org", SearchField.SERIES),
        ):
            page = await catalog.browse(CatalogRequest(query=query, search_field=field))
            assert [book.title for book in page.books] == ["Release v1.2.3"]


async def test_postgresql_import_search_materialization_activation_sequences_and_vacuum(
    app_config: AppConfig,
) -> None:
    archive_path = app_config.catalog.archive_root / "nested" / "books.zip"
    archive_path.parent.mkdir()
    archive_path.touch()
    _write_inpx(
        app_config.catalog.inpx_path,
        _line(),
        _line(FILE="hidden", TITLE="Hidden title", DEL="1"),
    )

    async with _catalog(app_config) as (repository, coordinator):
        await repository.check_readiness()
        assert (await repository.active_snapshot()).generation_id is None
        assert (await repository.catalog_statistics(None)).database_size_bytes > 0

        result = await coordinator._request(ImportTrigger.MANUAL, force=True)
        assert result.outcome is ImportOutcome.IMPORTED
        assert result.status is not None
        assert result.status.generation_id is not None
        generation_id = result.status.generation_id
        assert (
            result.status.records_read,
            result.status.records_imported,
            result.status.records_deleted,
        ) == (2, 1, 1)

        _, projection_rows = await repository._connection.execute_query(
            "SELECT title, authors, series, "
            "title_vector @@ plainto_tsquery('simple'::regconfig, 'cafe') AS title_match, "
            "all_vector @@ plainto_tsquery('simple'::regconfig, 'ru') AS language_match "
            "FROM book_fts ORDER BY book_id"
        )
        assert projection_rows[0]["title"] == "cafe resume naive елка"
        assert projection_rows[0]["authors"] == "jose alvarez иван елкин"
        assert projection_rows[0]["series"] == "serie"
        assert projection_rows[0]["title_match"] is True
        assert projection_rows[0]["language_match"] is False

        _, summary_rows = await repository._connection.execute_query(
            "SELECT visible_book_count, hidden_book_count FROM catalog_generation WHERE id=$1",
            [generation_id],
        )
        assert dict(summary_rows[0]) == {"visible_book_count": 1, "hidden_book_count": 1}
        _, archive_rows = await repository._connection.execute_query(
            "SELECT visible_book_count FROM archive WHERE generation_id=$1", [generation_id]
        )
        assert int(archive_rows[0]["visible_book_count"]) == 1

        catalog = CatalogService(repository, b"postgresql-test-cursor-key")
        for query, field in (
            ("cafe", SearchField.ALL),
            ("resume", SearchField.TITLE),
            ("jose", SearchField.AUTHOR),
            ("serie", SearchField.SERIES),
            ("Елка", SearchField.ALL),
        ):
            page = await catalog.browse(CatalogRequest(query=query, search_field=field))
            assert [book.title for book in page.books] == ["Café résumé naïve Ёлка"]
        assert (await catalog.browse(CatalogRequest(query="ru"))).books == ()
        unfiltered_page = await catalog.browse(CatalogRequest())
        detail = await catalog.details(unfiltered_page.books[0].public_id)
        assert detail is not None
        assert detail.title == "Café résumé naïve Ёлка"
        assert detail.authors[0] == "José Álvarez"

        statistics = await catalog.statistics()
        assert (statistics.total_books, statistics.hidden_books, statistics.active_books) == (
            2,
            1,
            1,
        )
        archive_path.unlink()
        await coordinator.refresh_archive_availability()
        assert (await catalog.statistics()).missed_books == 1
        assert (await catalog.browse(CatalogRequest(query="cafe"))).books == ()
        assert (
            len((await catalog.browse(CatalogRequest(query="cafe", include_missed=True))).books)
            == 1
        )
        archive_path.touch()
        await coordinator.refresh_archive_availability()
        previous_revision = (await repository.active_snapshot()).updated_at

        second = await coordinator._request(ImportTrigger.MANUAL, force=True)
        assert second.outcome is ImportOutcome.IMPORTED
        second_snapshot = await repository.active_snapshot()
        assert second_snapshot.updated_at > previous_revision
        _, generation_rows = await repository._connection.execute_query(
            "SELECT id, state FROM catalog_generation ORDER BY id"
        )
        assert [(int(row["id"]), row["state"]) for row in generation_rows] == [
            (second_snapshot.generation_id, "active")
        ]

        maxima = await repository.id_counters()
        generated_archive = await Archive.create(
            using_db=repository._connection,
            generation_id=second_snapshot.generation_id,
            relative_path="generated.zip",
        )
        generated_author = await Author.create(
            using_db=repository._connection,
            generation_id=second_snapshot.generation_id,
            name="Generated author",
            name_sort="generated author",
        )
        generated_genre = await Genre.create(
            using_db=repository._connection,
            generation_id=second_snapshot.generation_id,
            code="generated",
            label="Generated",
            label_sort="generated",
        )
        generated_series = await Series.create(
            using_db=repository._connection,
            generation_id=second_snapshot.generation_id,
            name="Generated series",
            name_sort="generated series",
        )
        generated_book = await Book.create(
            using_db=repository._connection,
            generation_id=second_snapshot.generation_id,
            public_id="generated-public-id",
            archive=generated_archive,
            member_filename="generated.fb2",
            title="Generated",
            title_sort="generated",
            series=generated_series,
            size=1,
            original_format="fb2",
        )
        generated_book_author = await BookAuthor.create(
            using_db=repository._connection,
            book=generated_book,
            author=generated_author,
            position=0,
        )
        generated_book_genre = await BookGenre.create(
            using_db=repository._connection,
            book=generated_book,
            genre=generated_genre,
        )
        assert (
            generated_archive.id > maxima.archive
            and generated_author.id > maxima.author
            and generated_genre.id > maxima.genre
            and generated_series.id > maxima.series
            and generated_book.id > maxima.book
            and generated_book_author.id > maxima.book_author
            and generated_book_genre.id > maxima.book_genre
        )

        assert await coordinator.vacuum_database()
        _, vacuum_rows = await repository._connection.execute_query(
            "SELECT last_vacuum IS NOT NULL AS vacuumed, "
            "last_analyze IS NOT NULL AS analyzed FROM pg_stat_user_tables WHERE relname='book'"
        )
        assert dict(vacuum_rows[0]) == {"vacuumed": True, "analyzed": True}


async def test_catalog_state_lock_serializes_activation_cleanup_and_cancelled_availability(
    app_config: AppConfig,
) -> None:
    async with _catalog(app_config) as (repository, _coordinator):
        active = await CatalogGeneration.create(
            using_db=repository._connection,
            state=GenerationState.ACTIVE,
        )
        old_archive = await Archive.create(
            using_db=repository._connection,
            generation=active,
            relative_path="old.zip",
            available=False,
        )
        await (
            CatalogState.filter(id=1)
            .using_db(repository._connection)
            .update(active_generation_id=active.id)
        )
        await repository.ensure_source("default", app_config.catalog.inpx_path)
        fingerprint = SourceFingerprint(1, 1, "b" * 64)
        run_id, generation_id = await repository.create_import(ImportTrigger.MANUAL, fingerprint)

        blocker = await asyncpg.connect(app_config.database.url.get_secret_value())
        transaction = blocker.transaction()
        try:
            await transaction.start()
            await blocker.fetchrow("SELECT id FROM catalog_state WHERE id=1 FOR UPDATE")
            activation = asyncio.create_task(
                repository.activate(run_id, generation_id, fingerprint, (0, 0, 0, 0))
            )
            availability = asyncio.create_task(
                repository.update_archive_availability({old_archive.id: True})
            )
            cleanup = asyncio.create_task(repository.cleanup_inactive())
            try:
                waiters = await _wait_for_catalog_state_lock_waiters(blocker, 3)
                availability.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await availability
                remaining_waiters = await _wait_for_catalog_state_lock_waiters(blocker, 2)
                assert remaining_waiters < waiters

                await transaction.commit()
                await activation
                await cleanup
            finally:
                for task in (activation, availability, cleanup):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(activation, availability, cleanup, return_exceptions=True)
        finally:
            if blocker.is_in_transaction():
                await transaction.rollback()
            await blocker.close()
        await repository.cleanup_inactive()

        snapshot = await repository.active_snapshot()
        assert snapshot.generation_id == generation_id
        assert (
            not await CatalogGeneration.filter(id=active.id)
            .using_db(repository._connection)
            .exists()
        )
        run = await ImportRun.filter(id=run_id).using_db(repository._connection).get()
        assert run.state is ImportState.SUCCEEDED


async def test_failed_import_and_interrupted_recovery_preserve_active_generation(
    app_config: AppConfig,
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _catalog(app_config) as (repository, coordinator):
        first = await coordinator._request(ImportTrigger.MANUAL, force=True)
        assert first.outcome is ImportOutcome.IMPORTED
        active_id = (await repository.active_snapshot()).generation_id
        assert active_id is not None

        _write_inpx(app_config.catalog.inpx_path, _line(), _line(TITLE="Duplicate locator"))
        failed = await coordinator._request(ImportTrigger.MANUAL, force=True)
        assert failed.outcome is ImportOutcome.FAILED
        assert (await repository.active_snapshot()).generation_id == active_id
        assert failed.status is not None
        assert failed.status.error_summary == "Catalog database rejected imported data"
        _, active_projection_rows = await repository._connection.execute_query(
            "SELECT COUNT(*) AS count FROM book_fts WHERE generation_id=$1", [active_id]
        )
        assert int(active_projection_rows[0]["count"]) == 1

        run_id, interrupted_generation_id = await repository.create_import(
            ImportTrigger.SCHEDULED, SourceFingerprint(1, 1, "a" * 64)
        )
        for index in range(3):
            archive = await Archive.create(
                using_db=repository._connection,
                generation_id=interrupted_generation_id,
                relative_path=f"interrupted-{index}.zip",
            )
            book = await Book.create(
                using_db=repository._connection,
                generation_id=interrupted_generation_id,
                public_id=f"interrupted-{index}",
                archive=archive,
                member_filename=f"interrupted-{index}.fb2",
                title="Interrupted",
                title_sort="interrupted",
                size=1,
                original_format="fb2",
            )
            await repository._connection.execute_query(
                "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
                "VALUES ($1,$2,'interrupted','','','','')",
                [book.id, interrupted_generation_id],
            )

        recovery = await repository.recover()
        assert recovery.interrupted_runs == 1
        assert recovery.failed_generations == 1
        assert recovery.removed_generations == 1
        assert (await repository.active_snapshot()).generation_id == active_id
        assert (
            not await CatalogGeneration.filter(id=interrupted_generation_id)
            .using_db(repository._connection)
            .exists()
        )
        recovered_run = await ImportRun.filter(id=run_id).using_db(repository._connection).get()
        assert recovered_run.state is ImportState.INTERRUPTED
        staging_values = await (
            ImportRun.filter(id=run_id)
            .using_db(repository._connection)
            .values("staging_generation_id")
        )
        assert staging_values[0]["staging_generation_id"] is None
        _, stale_projection_rows = await repository._connection.execute_query(
            "SELECT COUNT(*) AS count FROM book_fts WHERE generation_id=$1",
            [interrupted_generation_id],
        )
        assert int(stale_projection_rows[0]["count"]) == 0
