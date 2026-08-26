"""Generation import, coordination, recovery, and projection tests."""

import asyncio
import os
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from tortoise.context import TortoiseContext
from tortoise.queryset import QuerySet

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
from sopds.db.repository import CatalogRepository, IdCounters
from sopds.db.rows import BookRow, CatalogWriteBatch
from sopds.imports import fingerprint as fingerprint_module
from sopds.imports import service as service_module
from sopds.imports.coordinator import ImportCoordinator
from sopds.imports.fingerprint import (
    SourceFingerprint,
    SourceUnstableError,
    hash_source,
    stat_source,
)
from sopds.imports.inpx import InpxRecord
from sopds.imports.service import CatalogImportService, derive_public_id, normalize_sort_key
from sopds.imports.status import ImportOutcome, ImportResult, ImportState, ImportTrigger

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
        "AUTHOR": "Иван Ёлкин:Jane Doe:",
        "GENRE": "sf:prose:",
        "TITLE": "Ёжик",
        "SERIES": "Серия",
        "SERNO": "A-2",
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


def _write_inpx(path: Path, *lines: bytes, entry: str = "nested/books.inp") -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(entry, b"".join(lines))


@asynccontextmanager
async def _coordinator(
    config: AppConfig, *, batch_size: int = 2
) -> AsyncIterator[tuple[ImportCoordinator, TortoiseContext]]:
    await apply_migrations(config.database.path)
    context = await initialize_database(config.database.path)
    coordinator = ImportCoordinator(
        CatalogRepository(context.db()),
        config.catalog.inpx_path,
        config.catalog.archive_root,
        batch_size=batch_size,
    )
    await coordinator.recover()
    try:
        yield coordinator, context
    finally:
        await close_database(context)


def _query(path: Path, sql: str) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        return connection.execute(sql).fetchall()


def _catalog_snapshot(path: Path) -> dict[str, list[tuple[object, ...]]]:
    """Compare persisted projections independently of generated database IDs."""
    queries = {
        "archives": "SELECT relative_path,available FROM archive ORDER BY relative_path",
        "authors": "SELECT name,name_sort FROM author ORDER BY name_sort,name",
        "genres": "SELECT code,label,label_sort FROM genre ORDER BY code",
        "series": "SELECT name,name_sort FROM series ORDER BY name_sort,name",
        "books": (
            "SELECT public_id,member_filename,title,title_sort,series_number,size,libid,"
            "published_date,language,original_format,rating,keywords FROM book ORDER BY public_id"
        ),
        "book_authors": (
            "SELECT b.public_id,a.name,ba.position FROM book_author ba "
            "JOIN book b ON b.id=ba.book_id JOIN author a ON a.id=ba.author_id "
            "ORDER BY b.public_id,ba.position"
        ),
        "book_genres": (
            "SELECT b.public_id,g.code FROM book_genre bg "
            "JOIN book b ON b.id=bg.book_id JOIN genre g ON g.id=bg.genre_id "
            "ORDER BY b.public_id,g.code"
        ),
        "search": (
            "SELECT b.public_id,f.title,f.authors,f.series,f.genres,f.language FROM book_fts f "
            "JOIN book b ON b.id=f.book_id ORDER BY b.public_id"
        ),
    }
    return {name: _query(path, sql) for name, sql in queries.items()}


@pytest.mark.parametrize("batch_size", [0, -1])
def test_import_batch_size_must_be_positive_at_service_boundary(batch_size: int) -> None:
    repository = cast(CatalogRepository, object())
    source_path = Path("catalog.inpx")
    archive_root = Path("archives")

    with pytest.raises(ValueError, match="positive integer"):
        CatalogImportService(repository, source_path, archive_root, batch_size=batch_size)
    with pytest.raises(ValueError, match="positive integer"):
        ImportCoordinator(repository, source_path, archive_root, batch_size=batch_size)


async def test_named_book_row_fields_persist_through_orm_bulk_boundary(
    app_config: AppConfig,
) -> None:
    await apply_migrations(app_config.database.path)
    context = await initialize_database(app_config.database.path)
    connection = context.db()
    try:
        await CatalogGeneration.create(using_db=connection, id=11, state=GenerationState.IMPORTING)
        await Archive.create(
            using_db=connection,
            id=12,
            generation_id=11,
            relative_path="nested/books.zip",
            available=True,
        )
        await Series.create(
            using_db=connection,
            id=13,
            generation_id=11,
            name="Series",
            name_sort="series",
        )
        await CatalogRepository(connection).write_batch(
            CatalogWriteBatch(
                archives=(),
                authors=(),
                genres=(),
                series=(),
                books=(
                    BookRow(
                        id=14,
                        generation_id=11,
                        public_id="public",
                        archive_id=12,
                        member_filename="member.fb2",
                        title="Title",
                        title_sort="title",
                        series_id=13,
                        series_number=None,
                        size=15,
                        libid=None,
                        published_date=date(2024, 2, 3),
                        language=None,
                        original_format="fb2",
                        rating=None,
                        keywords=None,
                    ),
                ),
                book_authors=(),
                book_genres=(),
                search_rows=(),
            )
        )

        values = await (
            Book.filter(id=14)
            .using_db(connection)
            .values(
                "generation_id",
                "public_id",
                "archive_id",
                "member_filename",
                "title",
                "title_sort",
                "series_id",
                "series_number",
                "size",
                "libid",
                "published_date",
                "language",
                "original_format",
                "rating",
                "keywords",
            )
        )
        assert values == [
            {
                "generation_id": 11,
                "public_id": "public",
                "archive_id": 12,
                "member_filename": "member.fb2",
                "title": "Title",
                "title_sort": "title",
                "series_id": 13,
                "series_number": None,
                "size": 15,
                "libid": None,
                "published_date": date(2024, 2, 3),
                "language": None,
                "original_format": "fb2",
                "rating": None,
                "keywords": None,
            }
        ]
    finally:
        await close_database(context)


async def test_bulk_and_single_record_batches_persist_identical_catalogs(
    app_config: AppConfig,
) -> None:
    _write_inpx(
        app_config.catalog.inpx_path,
        _line(),
        _line(FILE="second", TITLE="Second", AUTHOR="Other:", GENRE="history:"),
        _line(FILE="third", TITLE="Third", SERIES="", SERNO="", LANG="en"),
    )
    async with _coordinator(app_config, batch_size=1) as (coordinator, _):
        assert (await coordinator.force_import()).outcome is ImportOutcome.IMPORTED
    single_record_batches = _catalog_snapshot(app_config.database.path)

    bulk_config = app_config.model_copy(
        update={
            "database": app_config.database.model_copy(
                update={"path": app_config.database.path.with_name("bulk.sqlite3")}
            )
        }
    )
    async with _coordinator(bulk_config, batch_size=2_000) as (coordinator, _):
        assert (await coordinator.force_import()).outcome is ImportOutcome.IMPORTED

    assert _catalog_snapshot(bulk_config.database.path) == single_record_batches


async def test_first_check_maps_full_rows_relations_fts_and_counters(app_config: AppConfig) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line(), _line(FILE="gone", DEL="1"))
    archive_path = app_config.catalog.archive_root / "nested" / "books.zip"
    archive_path.parent.mkdir()
    archive_path.touch()

    async with _coordinator(app_config) as (coordinator, _):
        result = await coordinator.check_for_changes()

    assert result.outcome is ImportOutcome.IMPORTED
    assert result.status is not None
    assert (
        result.status.records_read,
        result.status.records_imported,
        result.status.records_deleted,
    ) == (2, 1, 1)
    book = _query(
        app_config.database.path,
        "SELECT public_id,title,title_sort,series_number,size,libid,published_date,language,"
        "original_format,rating,keywords FROM book",
    )
    assert book == [
        (
            derive_public_id("default", "nested/books.zip", "book.fb2"),
            "Ёжик",
            "ежик",
            "A-2",
            123,
            "lib-1",
            "2024-02-03",
            "ru",
            "fb2",
            4,
            "one,two",
        )
    ]
    assert _query(
        app_config.database.path,
        "SELECT name,position FROM author JOIN book_author ON author.id=author_id ORDER BY position",
    ) == [("Иван Ёлкин", 0), ("Jane Doe", 1)]
    assert _query(app_config.database.path, "SELECT code FROM genre ORDER BY code") == [
        ("prose",),
        ("sf",),
    ]
    assert _query(
        app_config.database.path, "SELECT title,authors,series,genres,language FROM book_fts"
    ) == [("ежик", "иван елкин jane doe", "серия", "sf prose", "ru")]
    assert normalize_sort_key("  ЁЖ ") == "  еж "


async def test_fingerprint_fast_paths_and_forced_generation(app_config: AppConfig) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        first = await coordinator.check_for_changes()
        unchanged = await coordinator.check_for_changes()
        runs_after_unchanged = _query(app_config.database.path, "SELECT count(*) FROM import_run")
        stat = app_config.catalog.inpx_path.stat()
        os.utime(app_config.catalog.inpx_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        same_content = await coordinator.check_for_changes()
        forced = await coordinator.force_import()

    assert first.outcome is ImportOutcome.IMPORTED
    assert unchanged.outcome is ImportOutcome.UNCHANGED
    assert runs_after_unchanged == [(1,)]
    assert same_content.outcome is ImportOutcome.CONTENT_UNCHANGED
    assert forced.outcome is ImportOutcome.IMPORTED
    assert _query(app_config.database.path, "SELECT count(*) FROM import_run") == [(2,)]
    assert _query(
        app_config.database.path, "SELECT count(*) FROM catalog_generation WHERE state='active'"
    ) == [(1,)]


@pytest.mark.parametrize("failure", ["parser", "duplicate"])
async def test_failed_import_preserves_active_generation_and_fingerprint(
    app_config: AppConfig, failure: str
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        assert (await coordinator.check_for_changes()).outcome is ImportOutcome.IMPORTED
        before = _query(app_config.database.path, "SELECT active_generation_id FROM catalog_state")[
            0
        ][0]
        fingerprint = _query(
            app_config.database.path, "SELECT fingerprint_sha256 FROM catalog_source"
        )[0][0]
        if failure == "parser":
            _write_inpx(app_config.catalog.inpx_path, b"not-crlf")
        else:
            _write_inpx(app_config.catalog.inpx_path, _line(), _line(TITLE="other"))
        failed = await coordinator.check_for_changes()

    assert failed.outcome is ImportOutcome.FAILED
    assert _query(app_config.database.path, "SELECT active_generation_id FROM catalog_state") == [
        (before,)
    ]
    assert _query(app_config.database.path, "SELECT fingerprint_sha256 FROM catalog_source") == [
        (fingerprint,)
    ]
    assert _query(app_config.database.path, "SELECT count(*) FROM book_fts") == [(1,)]
    if failure == "parser":
        assert _query(
            app_config.database.path,
            "SELECT records_imported,records_rejected FROM import_run ORDER BY id DESC LIMIT 1",
        ) == [(0, 1)]


async def test_source_mutation_preserving_metadata_after_parse_prevents_activation(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    original_next = service_module._ParserWorker.next_batch
    mutated = False

    async def mutate_after_parse(worker: service_module._ParserWorker) -> list[InpxRecord]:
        nonlocal mutated
        batch = await original_next(worker)
        if not batch and not mutated:
            metadata = app_config.catalog.inpx_path.stat()
            contents = bytearray(app_config.catalog.inpx_path.read_bytes())
            contents[-1] ^= 1
            app_config.catalog.inpx_path.write_bytes(contents)
            os.utime(
                app_config.catalog.inpx_path,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
            )
            mutated = True
        return batch

    monkeypatch.setattr(service_module._ParserWorker, "next_batch", mutate_after_parse)
    async with _coordinator(app_config) as (coordinator, _):
        result = await coordinator.check_for_changes()

    assert result.outcome is ImportOutcome.FAILED
    assert _query(app_config.database.path, "SELECT active_generation_id FROM catalog_state") == [
        (None,)
    ]


@pytest.mark.parametrize("restore_metadata", [False, True])
async def test_source_mutation_during_count_validation_prevents_activation(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    *,
    restore_metadata: bool,
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        repository = coordinator._repository
        original_validate = repository.validate_generation_counts

        async def validate_then_mutate(generation_id: int, expected: int) -> None:
            await original_validate(generation_id, expected)
            metadata = app_config.catalog.inpx_path.stat()
            contents = bytearray(app_config.catalog.inpx_path.read_bytes())
            contents[-1] ^= 1
            app_config.catalog.inpx_path.write_bytes(contents)
            if restore_metadata:
                os.utime(
                    app_config.catalog.inpx_path,
                    ns=(metadata.st_atime_ns, metadata.st_mtime_ns),
                )

        monkeypatch.setattr(repository, "validate_generation_counts", validate_then_mutate)
        result = await coordinator.force_import()

    assert result.outcome is ImportOutcome.FAILED
    assert _query(app_config.database.path, "SELECT active_generation_id FROM catalog_state") == [
        (None,)
    ]


async def test_concurrent_request_is_not_queued(app_config: AppConfig) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        entered = asyncio.Event()
        release = asyncio.Event()
        original = coordinator._service.import_source

        async def blocked(trigger: ImportTrigger, fingerprint: SourceFingerprint) -> ImportResult:
            entered.set()
            await release.wait()
            return await original(trigger, fingerprint)

        coordinator._service.import_source = blocked  # type: ignore[method-assign]
        first_task = asyncio.create_task(coordinator.force_import())
        await entered.wait()
        second = await coordinator.check_for_changes()
        release.set()
        first = await first_task

    assert first.outcome is ImportOutcome.IMPORTED
    assert second.outcome is ImportOutcome.ALREADY_RUNNING
    assert _query(app_config.database.path, "SELECT count(*) FROM import_run") == [(1,)]


async def test_recovery_cleans_interrupted_generation_and_fts(app_config: AppConfig) -> None:
    await apply_migrations(app_config.database.path)
    context = await initialize_database(app_config.database.path)
    connection = context.db()
    generation = await CatalogGeneration.create(
        using_db=connection, state=GenerationState.IMPORTING
    )
    run = await ImportRun.create(
        using_db=connection,
        trigger=ImportTrigger.SCHEDULED,
        state=ImportState.RUNNING,
        staging_generation=generation,
    )
    await Archive.create(
        using_db=connection,
        generation=generation,
        relative_path="books.zip",
    )
    # FTS5 has no ORM model; recovery must still clean its orphanable projection rows.
    await connection.execute_query(
        "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
        "VALUES (1,?,'x','','','','')",
        [generation.id],
    )
    coordinator = ImportCoordinator(
        CatalogRepository(connection),
        app_config.catalog.inpx_path,
        app_config.catalog.archive_root,
    )
    await coordinator.recover()
    await close_database(context)

    assert run.id is not None
    assert _query(app_config.database.path, "SELECT state FROM import_run ORDER BY id") == [
        ("interrupted",)
    ]
    assert _query(app_config.database.path, "SELECT count(*) FROM catalog_generation") == [(0,)]
    assert _query(app_config.database.path, "SELECT count(*) FROM book_fts") == [(0,)]


async def test_archive_availability_updates_in_configured_chunks(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await apply_migrations(app_config.database.path)
    context = await initialize_database(app_config.database.path)
    connection = context.db()
    repository = CatalogRepository(connection, cleanup_batch_size=2)
    generation = await CatalogGeneration.create(using_db=connection, state=GenerationState.ACTIVE)
    archive_ids: list[int] = []
    for index in range(5):
        archive = await Archive.create(
            using_db=connection,
            generation=generation,
            relative_path=f"archive-{index}.zip",
            available=False,
        )
        archive_ids.append(archive.id)

    original_filter = Archive.filter
    original_all = Archive.all
    queried_chunks: list[tuple[int, ...]] = []
    bulk_update_chunks = 0

    def tracking_filter(_model: type[Archive], *args: Any, **kwargs: Any) -> QuerySet[Archive]:
        requested_ids = kwargs.get("id__in")
        if requested_ids is not None:
            queried_chunks.append(tuple(cast(list[int], requested_ids)))
        return original_filter(*args, **kwargs)

    def tracking_all(_model: type[Archive], *args: Any, **kwargs: Any) -> QuerySet[Archive]:
        nonlocal bulk_update_chunks
        bulk_update_chunks += 1
        return original_all(*args, **kwargs)

    monkeypatch.setattr(Archive, "filter", classmethod(tracking_filter))
    monkeypatch.setattr(Archive, "all", classmethod(tracking_all))
    await repository.update_archive_availability(dict.fromkeys(archive_ids, True))
    await close_database(context)

    assert queried_chunks == [
        tuple(archive_ids[0:2]),
        tuple(archive_ids[2:4]),
        tuple(archive_ids[4:5]),
    ]
    assert bulk_update_chunks == 3
    assert _query(app_config.database.path, "SELECT available FROM archive ORDER BY id") == [
        (1,),
        (1,),
        (1,),
        (1,),
        (1,),
    ]


async def test_archive_availability_refreshes_without_new_run(app_config: AppConfig) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    archive = app_config.catalog.archive_root / "nested" / "books.zip"
    async with _coordinator(app_config) as (coordinator, _):
        await coordinator.check_for_changes()
        assert _query(app_config.database.path, "SELECT available FROM archive") == [(0,)]
        archive.parent.mkdir(exist_ok=True)
        archive.touch()
        result = await coordinator.check_for_changes()

    assert result.outcome is ImportOutcome.UNCHANGED
    assert _query(app_config.database.path, "SELECT available FROM archive") == [(1,)]
    assert _query(app_config.database.path, "SELECT count(*) FROM import_run") == [(1,)]


async def test_source_identity_change_clears_matching_metadata_fingerprint(
    app_config: AppConfig,
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, context):
        assert (await coordinator.check_for_changes()).outcome is ImportOutcome.IMPORTED
        metadata = app_config.catalog.inpx_path.stat()
        replacement = app_config.catalog.inpx_path.with_name("replacement.inpx")
        replacement.write_bytes(app_config.catalog.inpx_path.read_bytes())
        os.utime(replacement, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        replacement_coordinator = ImportCoordinator(
            CatalogRepository(context.db()),
            replacement,
            app_config.catalog.archive_root,
        )
        await replacement_coordinator.recover()
        result = await replacement_coordinator.check_for_changes()

    assert result.outcome is ImportOutcome.IMPORTED
    assert _query(app_config.database.path, "SELECT count(*) FROM import_run") == [(2,)]


@pytest.mark.parametrize("stage", ["during_setup", "after_setup"])
async def test_cancellation_around_atomic_setup_cleans_known_state(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        repository = coordinator._repository
        entered = asyncio.Event()
        release = asyncio.Event()
        if stage == "during_setup":
            original_create_import = repository.create_import

            async def blocked_create_import(
                trigger: ImportTrigger, fingerprint: SourceFingerprint
            ) -> tuple[int, int]:
                entered.set()
                await release.wait()
                return await original_create_import(trigger, fingerprint)

            monkeypatch.setattr(repository, "create_import", blocked_create_import)
        else:
            original_id_counters = repository.id_counters

            async def blocked_id_counters() -> IdCounters:
                entered.set()
                await release.wait()
                return await original_id_counters()

            monkeypatch.setattr(repository, "id_counters", blocked_id_counters)
        task = asyncio.create_task(coordinator.force_import())
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert _query(app_config.database.path, "SELECT state FROM import_run") == [("interrupted",)]
    assert _query(app_config.database.path, "SELECT state FROM catalog_generation") == [("failed",)]


@pytest.mark.parametrize("stage", ["before", "during", "just_after"])
async def test_cancellation_around_activation_never_fails_an_activated_generation(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        repository = coordinator._repository
        entered = asyncio.Event()
        release = asyncio.Event()
        if stage == "before":
            original_validate = repository.validate_generation_counts

            async def blocked_validate(generation_id: int, expected: int) -> None:
                entered.set()
                await release.wait()
                await original_validate(generation_id, expected)

            monkeypatch.setattr(repository, "validate_generation_counts", blocked_validate)
        else:
            original_activate = repository.activate

            async def blocked_activate(*args: object, **kwargs: object) -> None:
                if stage == "during":
                    entered.set()
                    await release.wait()
                    await original_activate(*args, **kwargs)  # type: ignore[arg-type]
                else:
                    await original_activate(*args, **kwargs)  # type: ignore[arg-type]
                    entered.set()
                    await release.wait()

            monkeypatch.setattr(repository, "activate", blocked_activate)
        task = asyncio.create_task(coordinator.force_import())
        await entered.wait()
        task.cancel()
        if stage != "before":
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    state = _query(
        app_config.database.path,
        "SELECT r.state,g.state FROM import_run r JOIN catalog_generation g "
        "ON g.id=r.staging_generation_id",
    )
    active = _query(app_config.database.path, "SELECT active_generation_id FROM catalog_state")[0][
        0
    ]
    if stage == "before":
        assert state == [("interrupted", "failed")]
        assert active is None
    else:
        assert state == [("succeeded", "active")]
        assert active is not None


async def test_activation_failure_while_cancellation_is_pending_uses_failure_path(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    finalized_states: list[ImportState] = []
    async with _coordinator(app_config) as (coordinator, _):
        repository = coordinator._repository
        activation_entered = asyncio.Event()
        activation_release = asyncio.Event()
        original_finish_failed = repository.finish_failed

        async def failing_activate(
            run_id: int,
            generation_id: int,
            fingerprint: SourceFingerprint,
            counters: tuple[int, int, int, int],
        ) -> None:
            activation_entered.set()
            await activation_release.wait()
            raise sqlite3.OperationalError("activation failed")

        async def tracking_finish_failed(
            run_id: int,
            generation_id: int | None,
            state: ImportState,
            error_summary: str,
            counters: tuple[int, int, int, int],
        ) -> None:
            finalized_states.append(state)
            await original_finish_failed(run_id, generation_id, state, error_summary, counters)

        monkeypatch.setattr(repository, "activate", failing_activate)
        monkeypatch.setattr(repository, "finish_failed", tracking_finish_failed)
        task = asyncio.create_task(coordinator.force_import())
        await activation_entered.wait()
        task.cancel()
        activation_release.set()
        result = await task

    assert result.outcome is ImportOutcome.FAILED
    assert finalized_states == [ImportState.FAILED]
    assert _query(
        app_config.database.path, "SELECT count(*) FROM import_run WHERE state='running'"
    ) == [(0,)]
    assert _query(
        app_config.database.path,
        "SELECT count(*) FROM catalog_generation WHERE state='importing'",
    ) == [(0,)]


async def test_cancellation_during_failure_finalization_waits_for_database_outcome(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        repository = coordinator._repository
        finalization_entered = asyncio.Event()
        finalization_release = asyncio.Event()
        original_finish_failed = repository.finish_failed

        async def failing_validation(generation_id: int, expected: int) -> None:
            raise sqlite3.OperationalError("validation failed")

        async def blocked_finish_failed(
            run_id: int,
            generation_id: int | None,
            state: ImportState,
            error_summary: str,
            counters: tuple[int, int, int, int],
        ) -> None:
            finalization_entered.set()
            await finalization_release.wait()
            await original_finish_failed(run_id, generation_id, state, error_summary, counters)

        monkeypatch.setattr(repository, "validate_generation_counts", failing_validation)
        monkeypatch.setattr(repository, "finish_failed", blocked_finish_failed)
        task = asyncio.create_task(coordinator.force_import())
        await finalization_entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finalization_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert _query(app_config.database.path, "SELECT state FROM import_run") == [("failed",)]
    assert _query(app_config.database.path, "SELECT state FROM catalog_generation") == [("failed",)]


async def test_status_read_failure_after_activation_does_not_fail_catalog(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):

        async def broken_status() -> None:
            raise sqlite3.OperationalError("status unavailable")

        monkeypatch.setattr(coordinator._repository, "latest_status", broken_status)
        result = await coordinator.force_import()

    assert result == ImportResult(ImportOutcome.IMPORTED, None)
    assert _query(app_config.database.path, "SELECT state FROM import_run") == [("succeeded",)]
    assert _query(
        app_config.database.path,
        "SELECT g.state FROM catalog_generation g JOIN catalog_state s "
        "ON s.active_generation_id=g.id",
    ) == [("active",)]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("AUTHOR", "private" * 74 + ":"),
        ("GENRE", "private" * 19 + ":"),
        ("TITLE", "private" * 147),
        ("SERIES", "private" * 74),
        ("LIBID", "private" * 19),
        ("LANG", "private" * 5),
        ("EXT", "private" * 5),
        ("AUTHOR", "\ufdfa" * 29 + ":"),
        ("GENRE", "\ufdfa" * 15 + ":"),
        ("SERIES", "\ufdfa" * 29),
        ("TITLE", "\ufdfa" * 57),
    ],
)
async def test_oversize_mapped_metadata_fails_safely_without_activation(
    app_config: AppConfig, field: str, value: str
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line(**{field: value}))
    async with _coordinator(app_config) as (coordinator, _):
        result = await coordinator.force_import()

    assert result.outcome is ImportOutcome.FAILED
    assert result.status is not None
    assert result.status.state is ImportState.FAILED
    assert result.status.error_summary is not None
    assert "private" not in result.status.error_summary
    assert _query(app_config.database.path, "SELECT active_generation_id FROM catalog_state") == [
        (None,)
    ]
    assert _query(app_config.database.path, "SELECT count(*) FROM book") == [(0,)]


@pytest.mark.parametrize("field", ["AUTHOR", "GENRE", "TITLE", "SERIES", "LANG"])
async def test_nul_in_searchable_metadata_fails_safely_without_activation(
    app_config: AppConfig, field: str
) -> None:
    value = "visible\x00private"
    if field in {"AUTHOR", "GENRE"}:
        value += ":"
    _write_inpx(app_config.catalog.inpx_path, _line(**{field: value}))
    async with _coordinator(app_config) as (coordinator, _):
        result = await coordinator.force_import()

    assert result.outcome is ImportOutcome.FAILED
    assert result.status is not None
    assert result.status.state is ImportState.FAILED
    assert result.status.error_summary is not None
    assert "private" not in result.status.error_summary
    assert _query(app_config.database.path, "SELECT active_generation_id FROM catalog_state") == [
        (None,)
    ]
    assert _query(app_config.database.path, "SELECT count(*) FROM book_fts") == [(0,)]


async def test_nul_in_keywords_remains_lossless(app_config: AppConfig) -> None:
    keywords = "visible\x00private"
    _write_inpx(app_config.catalog.inpx_path, _line(KEYWORDS=keywords))
    async with _coordinator(app_config) as (coordinator, _):
        result = await coordinator.force_import()

    assert result.outcome is ImportOutcome.IMPORTED
    assert _query(app_config.database.path, "SELECT keywords FROM book") == [(keywords,)]


@pytest.mark.parametrize(
    ("lines", "batch_size", "expected"),
    [
        ((_line(), _line(TITLE="duplicate")), 2, (2, 0, 0, 1)),
        ((_line(), _line(FILE="bad-date", DATE="not-a-date")), 1, (2, 1, 0, 1)),
    ],
)
async def test_failed_batch_counters_include_only_committed_books(
    app_config: AppConfig,
    lines: tuple[bytes, ...],
    batch_size: int,
    expected: tuple[int, int, int, int],
) -> None:
    _write_inpx(app_config.catalog.inpx_path, *lines)
    async with _coordinator(app_config, batch_size=batch_size) as (coordinator, _):
        result = await coordinator.force_import()

    assert result.outcome is ImportOutcome.FAILED
    assert _query(
        app_config.database.path,
        "SELECT records_read,records_imported,records_deleted,records_rejected FROM import_run",
    ) == [expected]


async def test_projection_count_mismatch_prevents_activation(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        repository = coordinator._repository
        original = repository.validate_generation_counts

        async def corrupt_projection(generation_id: int, expected: int) -> None:
            await repository._connection.execute_query(
                "DELETE FROM book_fts WHERE generation_id=?", [generation_id]
            )
            await original(generation_id, expected)

        monkeypatch.setattr(repository, "validate_generation_counts", corrupt_projection)
        result = await coordinator.force_import()

    assert result.outcome is ImportOutcome.FAILED
    assert _query(app_config.database.path, "SELECT active_generation_id FROM catalog_state") == [
        (None,)
    ]


async def test_archive_symlink_escape_is_unavailable_on_import_and_refresh(
    app_config: AppConfig,
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    outside = app_config.catalog.archive_root.parent / "outside"
    outside.mkdir()
    (outside / "books.zip").touch()
    (app_config.catalog.archive_root / "nested").symlink_to(outside, target_is_directory=True)
    async with _coordinator(app_config) as (coordinator, _):
        assert (await coordinator.force_import()).outcome is ImportOutcome.IMPORTED
        imported = _query(app_config.database.path, "SELECT available FROM archive")
        await coordinator.refresh_archive_availability()
        refreshed = _query(app_config.database.path, "SELECT available FROM archive")

    assert imported == [(0,)]
    assert refreshed == [(0,)]


async def test_cleanup_inactive_deletes_large_generation_in_bounded_batches(
    app_config: AppConfig,
) -> None:
    await apply_migrations(app_config.database.path)
    context = await initialize_database(app_config.database.path)
    connection = context.db()
    repository = CatalogRepository(connection, cleanup_batch_size=2)
    active = await CatalogGeneration.create(using_db=connection, state=GenerationState.ACTIVE)
    stale = await CatalogGeneration.create(using_db=connection, state=GenerationState.FAILED)
    await CatalogState.filter(id=1).using_db(connection).update(active_generation_id=active.id)
    for index in range(5):
        archive = await Archive.create(
            using_db=connection,
            generation=stale,
            relative_path=f"archive-{index}.zip",
        )
        author = await Author.create(
            using_db=connection,
            generation=stale,
            name=f"author-{index}",
            name_sort=f"author-{index}",
        )
        genre = await Genre.create(
            using_db=connection,
            generation=stale,
            code=f"genre-{index}",
            label=f"genre-{index}",
            label_sort=f"genre-{index}",
        )
        series = await Series.create(
            using_db=connection,
            generation=stale,
            name=f"series-{index}",
            name_sort=f"series-{index}",
        )
        book = await Book.create(
            using_db=connection,
            generation=stale,
            public_id=f"public-{index}",
            archive=archive,
            member_filename=f"book-{index}.fb2",
            title="book",
            title_sort="book",
            series=series,
            size=1,
            original_format="fb2",
        )
        await BookAuthor.create(using_db=connection, book=book, author=author, position=0)
        await BookGenre.create(using_db=connection, book=book, genre=genre)
        # FTS5 is intentionally populated through its raw persistence boundary.
        await connection.execute_query(
            "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
            "VALUES (?,?,'book','','','','')",
            [book.id, stale.id],
        )
    await repository.cleanup_inactive()
    await close_database(context)

    for table in (
        "book_fts",
        "book_author",
        "book_genre",
        "book",
        "archive",
        "author",
        "genre",
        "series",
    ):
        assert _query(
            app_config.database.path,
            f"SELECT count(*) FROM {table}",  # noqa: S608
        ) == [(0,)]
    assert _query(app_config.database.path, "SELECT id,state FROM catalog_generation") == [
        (active.id, "active")
    ]


async def test_hash_rejects_metadata_instability(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    metadata = await stat_source(app_config.catalog.inpx_path)
    original_stat = fingerprint_module._stat
    calls = 0

    def changing_stat(path: Path) -> SourceFingerprint:
        nonlocal calls
        calls += 1
        actual = original_stat(path)
        if calls > 1:
            return SourceFingerprint(actual.size, actual.mtime_ns + 1)
        return actual

    monkeypatch.setattr(fingerprint_module, "_stat", changing_stat)
    with pytest.raises(SourceUnstableError):
        await hash_source(app_config.catalog.inpx_path, metadata)


async def test_repository_state_guards_cannot_fail_an_active_generation(
    app_config: AppConfig,
) -> None:
    await apply_migrations(app_config.database.path)
    context = await initialize_database(app_config.database.path)
    repository = CatalogRepository(context.db())
    await repository.ensure_source("default", app_config.catalog.inpx_path)
    fingerprint = SourceFingerprint(1, 1, "a" * 64)
    run_id, generation_id = await repository.create_import(ImportTrigger.MANUAL, fingerprint)
    await repository.activate(run_id, generation_id, fingerprint, (0, 0, 0, 0))
    await repository.finish_failed(
        run_id,
        generation_id,
        ImportState.FAILED,
        "late failure",
        (0, 0, 0, 0),
    )
    failed_run_id, failed_generation_id = await repository.create_import(
        ImportTrigger.MANUAL, fingerprint
    )
    await repository.finish_failed(
        failed_run_id,
        failed_generation_id,
        ImportState.FAILED,
        "pre-activation failure",
        (0, 0, 0, 0),
    )
    with pytest.raises(RuntimeError):
        await repository.activate(failed_run_id, failed_generation_id, fingerprint, (0, 0, 0, 0))
    await close_database(context)

    assert _query(
        app_config.database.path,
        "SELECT r.state,g.state FROM import_run r JOIN catalog_generation g "
        "ON g.id=r.staging_generation_id ORDER BY r.id",
    ) == [("succeeded", "active"), ("failed", "failed")]
    assert _query(app_config.database.path, "SELECT active_generation_id FROM catalog_state") == [
        (generation_id,)
    ]
