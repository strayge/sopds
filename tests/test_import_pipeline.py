"""Generation import, coordination, recovery, and projection tests."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, cast
from zipfile import ZIP_DEFLATED, ZipFile

import asyncpg  # type: ignore[import-untyped]
import pytest
from tortoise.context import TortoiseContext
from tortoise.queryset import QuerySet

from sopds.catalog.search import (
    SEARCH_PROJECTION_MAX_BYTES,
    bound_search_projection,
    normalize_search_projection,
)
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
    await apply_migrations(config.database)
    context = await initialize_database(config.database)
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


async def _run_forced_import(coordinator: ImportCoordinator) -> ImportResult:
    return await coordinator._request(ImportTrigger.MANUAL, force=True)


async def _query(config: AppConfig, sql: str) -> list[tuple[object, ...]]:
    connection = await asyncpg.connect(config.database.url.get_secret_value())
    try:
        rows = await connection.fetch(sql)
    finally:
        await connection.close()
    return [tuple(row.values()) for row in rows]


async def _catalog_snapshot(config: AppConfig) -> dict[str, list[tuple[object, ...]]]:
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
    return {name: await _query(config, sql) for name, sql in queries.items()}


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
    await apply_migrations(app_config.database)
    context = await initialize_database(app_config.database)
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
        assert (await _run_forced_import(coordinator)).outcome is ImportOutcome.IMPORTED
    single_record_batches = await _catalog_snapshot(app_config)

    await reset_test_database(app_config.database)
    async with _coordinator(app_config, batch_size=2_000) as (coordinator, _):
        assert (await _run_forced_import(coordinator)).outcome is ImportOutcome.IMPORTED

    assert await _catalog_snapshot(app_config) == single_record_batches


async def test_import_emits_start_progress_and_completion_logs(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(service_module, "_PROGRESS_RECORD_INTERVAL", 1)
    events: list[str] = []
    original_close = service_module._ParserWorker.close
    original_terminal = service_module._log_import_terminal

    async def observed_close(worker: service_module._ParserWorker) -> None:
        await original_close(worker)
        events.append("close")

    def observed_terminal(*args: Any, **kwargs: Any) -> None:
        events.append("terminal")
        original_terminal(*args, **kwargs)

    monkeypatch.setattr(service_module._ParserWorker, "close", observed_close)
    monkeypatch.setattr(service_module, "_log_import_terminal", observed_terminal)
    caplog.set_level("INFO", logger="sopds.imports")
    _write_inpx(app_config.catalog.inpx_path, _line(), _line(FILE="gone", DEL="1"))

    async with _coordinator(app_config) as (coordinator, _):
        result = await coordinator.check_for_changes()

    assert result.outcome is ImportOutcome.IMPORTED
    messages = [record.getMessage() for record in caplog.records]
    assert any("Catalog source check started" in message for message in messages)
    assert any("Catalog import started" in message for message in messages)
    assert any(
        "Catalog import progress" in message
        and "read=2" in message
        and "imported=1" in message
        and "deleted=1" in message
        for message in messages
    )
    phase_messages = (
        "Catalog import staging completed phase=staging",
        "Catalog import materialization started phase=materialization",
        "Catalog import materialization completed phase=materialization",
        "Catalog import validation started phase=validation",
        "Catalog import validation completed phase=validation",
        "Catalog source verification started phase=source_verification",
        "Catalog source verification completed phase=source_verification",
        "Catalog activation started phase=activation",
        "Catalog import activated phase=activation",
    )
    phase_positions = [
        next(index for index, message in enumerate(messages) if expected in message)
        for expected in phase_messages
    ]
    assert phase_positions == sorted(phase_positions)
    for position in (
        phase_positions[0],
        phase_positions[2],
        phase_positions[4],
        phase_positions[6],
        phase_positions[8],
    ):
        assert "duration_ms=" in messages[position]
    terminal_messages = [message for message in messages if "Catalog import finished" in message]
    assert len(terminal_messages) == 1
    assert "outcome=imported" in terminal_messages[0]
    assert "duration_ms=" in terminal_messages[0]
    assert events == ["close", "terminal"]
    assert "Ёжик" not in " ".join(messages)
    assert str(app_config.catalog.inpx_path) not in " ".join(messages)


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
    book = await _query(
        app_config,
        "SELECT public_id,title,title_sort,series_number,size,libid,published_date,language,"
        "original_format,rating,keywords,hidden FROM book ORDER BY id",
    )
    common = (
        "Ёжик",
        "ежик",
        "A-2",
        123,
        "lib-1",
        date(2024, 2, 3),
        "ru",
        "fb2",
        4,
        "one,two",
    )
    assert book == [
        (derive_public_id("default", "nested/books.zip", "book.fb2"), *common, False),
        (derive_public_id("default", "nested/books.zip", "gone.fb2"), *common, True),
    ]
    assert await _query(
        app_config,
        "SELECT visible_book_count,hidden_book_count FROM catalog_generation",
    ) == [(1, 1)]
    assert await _query(app_config, "SELECT visible_book_count FROM archive") == [(1,)]
    assert await _query(app_config, "SELECT language FROM archive_language") == [("ru",)]
    assert await _query(app_config, "SELECT original_format FROM archive_original_format") == [
        ("fb2",)
    ]
    assert await _query(app_config, "SELECT genre_id FROM archive_genre ORDER BY genre_id") == [
        (1,),
        (2,),
    ]
    assert await _query(
        app_config,
        "SELECT name,position FROM author JOIN book_author ON author.id=author_id "
        "ORDER BY book_id,position",
    ) == [
        ("Иван Ёлкин", 0),
        ("Jane Doe", 1),
        ("Иван Ёлкин", 0),
        ("Jane Doe", 1),
    ]
    assert await _query(app_config, "SELECT code,label FROM genre ORDER BY code") == [
        ("prose", "Проза"),
        ("sf", "Научная фантастика"),
    ]
    assert await _query(
        app_config, "SELECT title,authors,series,genres,language FROM book_fts"
    ) == [
        ("ежик", "иван елкин jane doe", "серия", "научная фантастика проза", "ru"),
        ("ежик", "иван елкин jane doe", "серия", "научная фантастика проза", "ru"),
    ]
    assert normalize_sort_key("  ЁЖ ") == "  еж "


async def test_fingerprint_fast_paths_and_forced_generation(app_config: AppConfig) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        first = await coordinator.check_for_changes()
        unchanged = await coordinator.check_for_changes()
        runs_after_unchanged = await _query(app_config, "SELECT count(*) FROM import_run")
        stat = app_config.catalog.inpx_path.stat()
        os.utime(app_config.catalog.inpx_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        same_content = await coordinator.check_for_changes()
        forced = await _run_forced_import(coordinator)

    assert first.outcome is ImportOutcome.IMPORTED
    assert unchanged.outcome is ImportOutcome.UNCHANGED
    assert runs_after_unchanged == [(1,)]
    assert same_content.outcome is ImportOutcome.CONTENT_UNCHANGED
    assert forced.outcome is ImportOutcome.IMPORTED
    assert await _query(app_config, "SELECT count(*) FROM import_run") == [(2,)]
    assert await _query(
        app_config, "SELECT count(*) FROM catalog_generation WHERE state='active'"
    ) == [(1,)]


@pytest.mark.parametrize("failure", ["parser", "duplicate"])
async def test_failed_import_preserves_active_generation_and_fingerprint(
    app_config: AppConfig, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        assert (await coordinator.check_for_changes()).outcome is ImportOutcome.IMPORTED
        before = (await _query(app_config, "SELECT active_generation_id FROM catalog_state"))[0][0]
        fingerprint = (await _query(app_config, "SELECT fingerprint_sha256 FROM catalog_source"))[
            0
        ][0]
        if failure == "parser":
            _write_inpx(app_config.catalog.inpx_path, b"not-crlf")
        else:
            _write_inpx(app_config.catalog.inpx_path, _line(), _line(TITLE="other"))
        caplog.clear()
        failed = await coordinator.check_for_changes()

    assert failed.outcome is ImportOutcome.FAILED
    terminal_messages = [
        record.getMessage()
        for record in caplog.records
        if "Catalog import finished" in record.getMessage()
    ]
    assert len(terminal_messages) == 1
    assert "outcome=failed" in terminal_messages[0]
    assert "duration_ms=" in terminal_messages[0]
    assert await _query(app_config, "SELECT active_generation_id FROM catalog_state") == [(before,)]
    assert await _query(app_config, "SELECT fingerprint_sha256 FROM catalog_source") == [
        (fingerprint,)
    ]
    assert await _query(app_config, "SELECT count(*) FROM book_fts") == [(1,)]
    if failure == "parser":
        assert await _query(
            app_config,
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
    assert await _query(app_config, "SELECT active_generation_id FROM catalog_state") == [(None,)]


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
        result = await _run_forced_import(coordinator)

    assert result.outcome is ImportOutcome.FAILED
    assert await _query(app_config, "SELECT active_generation_id FROM catalog_state") == [(None,)]


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
        first_task = asyncio.create_task(_run_forced_import(coordinator))
        await entered.wait()
        second = await coordinator.check_for_changes()
        release.set()
        first = await first_task

    assert first.outcome is ImportOutcome.IMPORTED
    assert second.outcome is ImportOutcome.ALREADY_RUNNING
    assert await _query(app_config, "SELECT count(*) FROM import_run") == [(1,)]


async def test_recovery_cleans_interrupted_generation_and_fts(
    app_config: AppConfig, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="sopds.imports.coordinator")
    await apply_migrations(app_config.database)
    context = await initialize_database(app_config.database)
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
    archive = await Archive.create(
        using_db=connection,
        generation=generation,
        relative_path="books.zip",
    )
    await Book.create(
        using_db=connection,
        id=1,
        generation=generation,
        public_id="interrupted",
        archive=archive,
        member_filename="interrupted.fb2",
        title="Interrupted",
        title_sort="interrupted",
        size=1,
        original_format="fb2",
    )
    # The search projection has no ORM model; recovery must still clean orphanable rows.
    await connection.execute_query(
        "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
        "VALUES (1,$1,'x','','','','')",
        [generation.id],
    )
    coordinator = ImportCoordinator(
        CatalogRepository(connection),
        app_config.catalog.inpx_path,
        app_config.catalog.archive_root,
    )
    await coordinator.recover()
    await close_database(context)

    messages = [record.getMessage() for record in caplog.records]
    started = messages.index("Catalog recovery started phase=recovery")
    completed = next(
        index
        for index, message in enumerate(messages)
        if message.startswith("Catalog recovery completed phase=recovery")
    )
    assert started < completed
    assert run.id is not None
    assert await _query(app_config, "SELECT state FROM import_run ORDER BY id") == [
        ("interrupted",)
    ]
    assert await _query(app_config, "SELECT count(*) FROM catalog_generation") == [(0,)]
    assert await _query(app_config, "SELECT count(*) FROM book_fts") == [(0,)]


async def test_archive_availability_updates_in_configured_chunks(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await apply_migrations(app_config.database)
    context = await initialize_database(app_config.database)
    connection = context.db()
    repository = CatalogRepository(connection, cleanup_batch_size=2)
    generation = await CatalogGeneration.create(using_db=connection, state=GenerationState.ACTIVE)
    await CatalogState.filter(id=1).using_db(connection).update(active_generation_id=generation.id)
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
    queried_chunks: list[tuple[int, ...]] = []

    def tracking_filter(_model: type[Archive], *args: Any, **kwargs: Any) -> QuerySet[Archive]:
        requested_ids = kwargs.get("id__in")
        if requested_ids is not None:
            queried_chunks.append(tuple(cast(list[int], requested_ids)))
        return original_filter(*args, **kwargs)

    monkeypatch.setattr(Archive, "filter", classmethod(tracking_filter))
    await repository.update_archive_availability(dict.fromkeys(archive_ids, True))
    await close_database(context)

    assert queried_chunks == [
        tuple(archive_ids[0:2]),
        tuple(archive_ids[2:4]),
        tuple(archive_ids[4:5]),
    ]
    assert await _query(app_config, "SELECT available FROM archive ORDER BY id") == [
        (True,),
        (True,),
        (True,),
        (True,),
        (True,),
    ]


async def test_archive_availability_refreshes_without_new_run(app_config: AppConfig) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    archive = app_config.catalog.archive_root / "nested" / "books.zip"
    async with _coordinator(app_config) as (coordinator, _):
        await coordinator.check_for_changes()
        assert await _query(app_config, "SELECT available FROM archive") == [(False,)]
        archive.parent.mkdir(exist_ok=True)
        archive.touch()
        result = await coordinator.check_for_changes()

    assert result.outcome is ImportOutcome.UNCHANGED
    assert await _query(app_config, "SELECT available FROM archive") == [(True,)]
    assert await _query(app_config, "SELECT count(*) FROM import_run") == [(1,)]


async def test_manual_import_refreshes_archive_availability_without_new_run(
    app_config: AppConfig,
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    archive = app_config.catalog.archive_root / "nested" / "books.zip"
    async with _coordinator(app_config) as (coordinator, _):
        await coordinator.check_for_changes()
        assert await _query(app_config, "SELECT available FROM archive") == [(False,)]
        archive.parent.mkdir(exist_ok=True)
        archive.touch()

        assert coordinator.start_manual_import()
        task = coordinator._manual_task
        assert task is not None
        result = await task

    assert result.outcome is ImportOutcome.UNCHANGED
    assert await _query(app_config, "SELECT available FROM archive") == [(True,)]
    assert await _query(app_config, "SELECT count(*) FROM import_run") == [(1,)]


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
    assert await _query(app_config, "SELECT count(*) FROM import_run") == [(2,)]


@pytest.mark.parametrize("stage", ["during_setup", "after_setup"])
async def test_cancellation_around_atomic_setup_cleans_known_state(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    caplog: pytest.LogCaptureFixture,
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
        task = asyncio.create_task(_run_forced_import(coordinator))
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert await _query(app_config, "SELECT state FROM import_run") == [("interrupted",)]
    assert await _query(app_config, "SELECT state FROM catalog_generation") == [("failed",)]
    terminal_messages = [
        record.getMessage()
        for record in caplog.records
        if "Catalog import finished" in record.getMessage()
    ]
    assert len(terminal_messages) == 1
    assert "outcome=interrupted" in terminal_messages[0]
    assert "duration_ms=" in terminal_messages[0]


async def test_repeated_cancellation_during_parser_cleanup_keeps_terminal_log(
    app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    entered = asyncio.Event()
    release = asyncio.Event()
    original_close = service_module._ParserWorker._run_close

    async def blocked_close(worker: service_module._ParserWorker) -> None:
        entered.set()
        await release.wait()
        await original_close(worker)

    monkeypatch.setattr(service_module._ParserWorker, "_run_close", blocked_close)
    caplog.set_level("INFO", logger="sopds.imports")
    async with _coordinator(app_config) as (coordinator, _):
        task = asyncio.create_task(_run_forced_import(coordinator))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    terminal_messages = [
        record.getMessage()
        for record in caplog.records
        if "Catalog import finished" in record.getMessage()
    ]
    assert len(terminal_messages) == 1
    assert "outcome=imported" in terminal_messages[0]


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
        task = asyncio.create_task(_run_forced_import(coordinator))
        await entered.wait()
        task.cancel()
        if stage != "before":
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    state = await _query(
        app_config,
        "SELECT r.state,g.state FROM import_run r JOIN catalog_generation g "
        "ON g.id=r.staging_generation_id",
    )
    active = (await _query(app_config, "SELECT active_generation_id FROM catalog_state"))[0][0]
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
            raise RuntimeError("activation failed")

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
        task = asyncio.create_task(_run_forced_import(coordinator))
        await activation_entered.wait()
        task.cancel()
        activation_release.set()
        result = await task

    assert result.outcome is ImportOutcome.FAILED
    assert finalized_states == [ImportState.FAILED]
    assert await _query(app_config, "SELECT count(*) FROM import_run WHERE state='running'") == [
        (0,)
    ]
    assert await _query(
        app_config,
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
            raise RuntimeError("validation failed")

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
        task = asyncio.create_task(_run_forced_import(coordinator))
        await finalization_entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        finalization_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert await _query(app_config, "SELECT state FROM import_run") == [("failed",)]
    assert await _query(app_config, "SELECT state FROM catalog_generation") == [("failed",)]


async def test_status_read_failure_after_activation_does_not_fail_catalog(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):

        async def broken_status() -> None:
            raise RuntimeError("status unavailable")

        monkeypatch.setattr(coordinator._repository, "latest_status", broken_status)
        result = await _run_forced_import(coordinator)

    assert result == ImportResult(ImportOutcome.IMPORTED, None)
    assert await _query(app_config, "SELECT state FROM import_run") == [("succeeded",)]
    assert await _query(
        app_config,
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
        ("SERNO", "private" * 19),
        ("KEYWORDS", "private" * 293),
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
        result = await _run_forced_import(coordinator)

    assert result.outcome is ImportOutcome.FAILED
    assert result.status is not None
    assert result.status.state is ImportState.FAILED
    assert result.status.error_summary is not None
    assert "private" not in result.status.error_summary
    assert await _query(app_config, "SELECT active_generation_id FROM catalog_state") == [(None,)]
    assert await _query(app_config, "SELECT count(*) FROM book") == [(0,)]


@pytest.mark.parametrize(
    "field", ["AUTHOR", "GENRE", "TITLE", "SERIES", "SERNO", "LANG", "KEYWORDS"]
)
async def test_nul_in_searchable_metadata_fails_safely_without_activation(
    app_config: AppConfig, field: str
) -> None:
    value = "visible\x00private"
    if field in {"AUTHOR", "GENRE"}:
        value += ":"
    _write_inpx(app_config.catalog.inpx_path, _line(**{field: value}))
    async with _coordinator(app_config) as (coordinator, _):
        result = await _run_forced_import(coordinator)

    assert result.outcome is ImportOutcome.FAILED
    assert result.status is not None
    assert result.status.state is ImportState.FAILED
    assert result.status.error_summary is not None
    assert "private" not in result.status.error_summary
    assert await _query(app_config, "SELECT active_generation_id FROM catalog_state") == [(None,)]
    assert await _query(app_config, "SELECT count(*) FROM book_fts") == [(0,)]


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
        result = await _run_forced_import(coordinator)

    assert result.outcome is ImportOutcome.FAILED
    assert await _query(
        app_config,
        "SELECT records_read,records_imported,records_deleted,records_rejected FROM import_run",
    ) == [expected]


async def test_extreme_valid_metadata_has_a_bounded_search_projection(
    app_config: AppConfig,
) -> None:
    author_names = tuple(
        f"{'earlymarker ' if index == 0 else ''}author{index} " + "слово " * 70
        for index in range(80)
    )
    authors = ":".join(author_names) + ":"
    _write_inpx(app_config.catalog.inpx_path, _line(AUTHOR=authors))

    async with _coordinator(app_config) as (coordinator, context):
        result = await _run_forced_import(coordinator)
        assert result.outcome is ImportOutcome.IMPORTED
        _, rows = await context.db().execute_query(
            "SELECT title,authors,series,genres,language,octet_length(authors) AS author_bytes "
            "FROM book_fts"
        )
        projection = dict(rows[0])
        expected_authors = bound_search_projection(
            normalize_search_projection(" ".join(author_names))
        )
        assert projection["authors"] == expected_authors
        assert projection["author_bytes"] == len(expected_authors.encode("utf-8"))
        assert 0 < int(projection["author_bytes"]) <= SEARCH_PROJECTION_MAX_BYTES
        assert len(normalize_search_projection(" ".join(author_names)).encode("utf-8")) > (
            SEARCH_PROJECTION_MAX_BYTES
        )
        for field in ("title", "authors", "series", "genres", "language"):
            value = str(projection[field])
            assert len(value.encode("utf-8")) <= SEARCH_PROJECTION_MAX_BYTES
            assert value == " ".join(value.split())
        assert await _query(
            app_config,
            "SELECT all_vector @@ plainto_tsquery('simple', 'earlymarker') FROM book_fts",
        ) == [(True,)]


async def test_projection_count_mismatch_prevents_activation(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    async with _coordinator(app_config) as (coordinator, _):
        repository = coordinator._repository
        original = repository.validate_generation_counts

        async def corrupt_projection(generation_id: int, expected: int) -> None:
            await repository._connection.execute_query(
                "DELETE FROM book_fts WHERE generation_id=$1", [generation_id]
            )
            await original(generation_id, expected)

        monkeypatch.setattr(repository, "validate_generation_counts", corrupt_projection)
        result = await _run_forced_import(coordinator)

    assert result.outcome is ImportOutcome.FAILED
    assert await _query(app_config, "SELECT active_generation_id FROM catalog_state") == [(None,)]


async def test_archive_symlink_escape_is_unavailable_on_import_and_refresh(
    app_config: AppConfig,
) -> None:
    _write_inpx(app_config.catalog.inpx_path, _line())
    outside = app_config.catalog.archive_root.parent / "outside"
    outside.mkdir()
    (outside / "books.zip").touch()
    (app_config.catalog.archive_root / "nested").symlink_to(outside, target_is_directory=True)
    async with _coordinator(app_config) as (coordinator, _):
        assert (await _run_forced_import(coordinator)).outcome is ImportOutcome.IMPORTED
        imported = await _query(app_config, "SELECT available FROM archive")
        await coordinator.refresh_archive_availability()
        refreshed = await _query(app_config, "SELECT available FROM archive")

    assert imported == [(False,)]
    assert refreshed == [(False,)]


async def test_cleanup_inactive_deletes_large_generation_in_bounded_batches(
    app_config: AppConfig,
) -> None:
    await apply_migrations(app_config.database)
    context = await initialize_database(app_config.database)
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
        # The generated-vector projection is populated through its raw persistence boundary.
        await connection.execute_query(
            "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
            "VALUES ($1,$2,'book','','','','')",
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
        assert await _query(
            app_config,
            f"SELECT count(*) FROM {table}",  # noqa: S608
        ) == [(0,)]
    assert await _query(app_config, "SELECT id,state FROM catalog_generation") == [
        (active.id, "active")
    ]


async def test_cleanup_commits_only_one_bounded_batch_before_cancellation(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await apply_migrations(app_config.database)
    context = await initialize_database(app_config.database)
    connection = context.db()
    repository = CatalogRepository(connection, cleanup_batch_size=2)
    stale = await CatalogGeneration.create(using_db=connection, state=GenerationState.FAILED)
    archive = await Archive.create(
        using_db=connection, generation=stale, relative_path="cancelled-cleanup.zip"
    )
    for index in range(5):
        book = await Book.create(
            using_db=connection,
            generation=stale,
            public_id=f"cancelled-cleanup-{index}",
            archive=archive,
            member_filename=f"cancelled-cleanup-{index}.fb2",
            title="book",
            title_sort="book",
            size=1,
            original_format="fb2",
        )
        await connection.execute_query(
            "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
            "VALUES ($1,$2,'book','','','','')",
            [book.id, stale.id],
        )

    original_delete = repository._delete_fts_batch
    batch_committed = asyncio.Event()
    hold_after_commit = asyncio.Event()

    async def pause_after_first_batch(generation_id: int) -> int | None:
        deleted = await original_delete(generation_id)
        if deleted:
            batch_committed.set()
            await hold_after_commit.wait()
        return deleted

    monkeypatch.setattr(repository, "_delete_fts_batch", pause_after_first_batch)
    cleanup = asyncio.create_task(repository.cleanup_inactive())
    try:
        await asyncio.wait_for(batch_committed.wait(), timeout=2)
        assert await _query(app_config, "SELECT count(*) FROM book_fts") == [(3,)]
        assert await _query(app_config, "SELECT count(*) FROM book") == [(5,)]
        cleanup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
    finally:
        if not cleanup.done():
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
        await close_database(context)


async def test_cleanup_revalidates_active_generation_before_each_batch(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    await apply_migrations(app_config.database)
    context = await initialize_database(app_config.database)
    connection = context.db()
    repository = CatalogRepository(connection, cleanup_batch_size=2)
    active = await CatalogGeneration.create(using_db=connection, state=GenerationState.ACTIVE)
    stale = await CatalogGeneration.create(using_db=connection, state=GenerationState.FAILED)
    await CatalogState.filter(id=1).using_db(connection).update(active_generation_id=active.id)
    archive = await Archive.create(
        using_db=connection, generation=stale, relative_path="revalidated-cleanup.zip"
    )
    for index in range(3):
        book = await Book.create(
            using_db=connection,
            generation=stale,
            public_id=f"revalidated-cleanup-{index}",
            archive=archive,
            member_filename=f"revalidated-cleanup-{index}.fb2",
            title="book",
            title_sort="book",
            size=1,
            original_format="fb2",
        )
        await connection.execute_query(
            "INSERT INTO book_fts(book_id,generation_id,title,authors,series,genres,language) "
            "VALUES ($1,$2,'book','','','','')",
            [book.id, stale.id],
        )

    original_delete = repository._delete_fts_batch
    activated = False

    async def activate_after_first_batch(generation_id: int) -> int | None:
        nonlocal activated
        deleted = await original_delete(generation_id)
        if deleted and not activated:
            activated = True
            await (
                CatalogState.filter(id=1).using_db(connection).update(active_generation_id=stale.id)
            )
        return deleted

    monkeypatch.setattr(repository, "_delete_fts_batch", activate_after_first_batch)
    try:
        summary = await repository.cleanup_inactive()
        assert summary.removed_generations == 0
        assert await CatalogGeneration.filter(id=stale.id).using_db(connection).exists()
        assert (
            await CatalogState.filter(id=1, active_generation_id=stale.id)
            .using_db(connection)
            .exists()
        )
        _, rows = await connection.execute_query(
            "SELECT count(*) AS count FROM book_fts WHERE generation_id=$1", [stale.id]
        )
        assert int(rows[0]["count"]) == 1
        assert await Book.filter(generation_id=stale.id).using_db(connection).count() == 3
    finally:
        await close_database(context)


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
    await apply_migrations(app_config.database)
    context = await initialize_database(app_config.database)
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

    assert await _query(
        app_config,
        "SELECT r.state,g.state FROM import_run r JOIN catalog_generation g "
        "ON g.id=r.staging_generation_id ORDER BY r.id",
    ) == [("succeeded", "active"), ("failed", "failed")]
    assert await _query(app_config, "SELECT active_generation_id FROM catalog_state") == [
        (generation_id,)
    ]
