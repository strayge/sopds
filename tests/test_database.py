"""Native migration and SQLite runtime integration tests."""

import sqlite3
from pathlib import Path
from typing import cast

import pytest
from tortoise.context import TortoiseContext
from tortoise.exceptions import IntegrityError

import sopds.db.connection as database_connection
from sopds.db.configuration import SQLITE_BUSY_TIMEOUT_MS
from sopds.db.connection import close_database, initialize_database
from sopds.db.migrations_runner import (
    MigrationError,
    PendingMigrationsError,
    apply_migrations,
    validate_migration_state,
)
from sopds.db.models import (
    Archive,
    Author,
    Book,
    BookAuthor,
    BookGenre,
    CatalogGeneration,
    GenerationState,
    Genre,
)

RELATION_INDEXES = {
    ("archive", ("generation_id", "available")),
    ("author", ("generation_id", "name_sort", "id")),
    ("book", ("generation_id", "title_sort", "public_id")),
    ("book", ("generation_id", "series_id", "series_number", "public_id")),
    ("book", ("generation_id", "language", "title_sort", "public_id")),
    ("book", ("generation_id", "libid")),
    ("book_author", ("author_id", "book_id")),
    ("book_genre", ("genre_id", "book_id")),
    ("genre", ("generation_id", "label_sort", "id")),
    ("series", ("generation_id", "name_sort", "id")),
}

RELATIONAL_TABLES = {
    "archive",
    "author",
    "book",
    "book_author",
    "book_genre",
    "catalog_generation",
    "catalog_source",
    "catalog_state",
    "genre",
    "import_run",
    "series",
    "tortoise_migrations",
}


@pytest.mark.asyncio
async def test_fresh_migration_is_idempotent(tmp_path: Path) -> None:
    database_path = tmp_path / "nested" / "catalog.sqlite3"

    await apply_migrations(database_path)
    await apply_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        history = connection.execute(
            "SELECT app, name FROM tortoise_migrations ORDER BY name"
        ).fetchall()
    assert history == [
        ("catalog", "0001_initial"),
        ("catalog", "0002_fts5"),
    ]


@pytest.mark.asyncio
async def test_migrations_create_relational_and_fts_schema(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

    assert tables >= RELATIONAL_TABLES
    assert "book_fts" in tables
    assert {"book_fts_data", "book_fts_idx", "book_fts_content", "book_fts_docsize"} <= tables


@pytest.mark.asyncio
async def test_migration_indexes_reference_real_table_columns(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)

    found_relation_indexes: set[tuple[str, tuple[str, ...]]] = set()
    with sqlite3.connect(database_path) as connection:
        indexes = connection.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
        for index_name, table_name in indexes:
            table_columns = {
                row[1]
                for row in connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
            }
            indexed_columns = tuple(
                row[2]
                for row in connection.execute(f'PRAGMA index_info("{index_name}")').fetchall()
            )
            assert indexed_columns
            assert all(column is not None for column in indexed_columns)
            assert set(indexed_columns) <= table_columns
            candidate = (table_name, indexed_columns)
            if candidate in RELATION_INDEXES:
                found_relation_indexes.add(candidate)

    assert found_relation_indexes == RELATION_INDEXES


@pytest.mark.asyncio
async def test_singleton_tables_reject_noncanonical_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT id, active_generation_id FROM catalog_state"
        ).fetchall() == [(1, None)]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO catalog_source(id, namespace, path, updated_at) "
                "VALUES (2, 'other', '/other.inpx', CURRENT_TIMESTAMP)"
            )


@pytest.mark.asyncio
async def test_fts_projection_supports_match_queries(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO book_fts(
                book_id, generation_id, title, authors, series, genres, language
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (7, 3, "Тестовая книга", "Иван Автор", "Цикл", "Фантастика", "ru"),
        )
        result = connection.execute(
            "SELECT book_id, generation_id FROM book_fts WHERE book_fts MATCH ?",
            ("тестовая",),
        ).fetchall()

    assert result == [(7, 3)]


@pytest.mark.asyncio
async def test_runtime_connection_has_required_sqlite_pragmas(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)
    context = await initialize_database(database_path)
    try:
        connection = context.db()
        _, journal_mode = await connection.execute_query("PRAGMA journal_mode")
        _, foreign_keys = await connection.execute_query("PRAGMA foreign_keys")
        _, busy_timeout = await connection.execute_query("PRAGMA busy_timeout")
    finally:
        await close_database(context)

    assert str(journal_mode[0]["journal_mode"]).lower() == "wal"
    assert foreign_keys[0]["foreign_keys"] == 1
    assert busy_timeout[0]["timeout"] == SQLITE_BUSY_TIMEOUT_MS


@pytest.mark.asyncio
async def test_runtime_connection_enforces_foreign_keys(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)
    context = await initialize_database(database_path)
    try:
        with pytest.raises(IntegrityError):
            await context.db().execute_query(
                "UPDATE catalog_state SET active_generation_id = 999, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = 1"
            )
    finally:
        await close_database(context)


@pytest.mark.asyncio
async def test_book_round_trips_parser_compatible_optional_values(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)
    context = await initialize_database(database_path)
    try:
        generation = await CatalogGeneration.create(state=GenerationState.IMPORTING)
        archive = await Archive.create(
            generation=generation,
            relative_path="books.zip",
        )
        book = await Book.create(
            generation=generation,
            public_id="parser-id",
            archive=archive,
            member_filename="book.fb2",
            title="Book",
            title_sort="book",
            series_number="том 2-A",
            size=123,
            libid=None,
            language=None,
            original_format="fb2",
            rating=5,
        )

        loaded = await Book.get(id=book.id)
    finally:
        await close_database(context)

    assert loaded.series_number == "том 2-A"
    assert loaded.libid is None
    assert loaded.language is None
    assert loaded.rating == 5


@pytest.mark.asyncio
async def test_deleting_generation_cascades_all_relational_rows(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)
    context = await initialize_database(database_path)
    try:
        generation = await CatalogGeneration.create(state=GenerationState.IMPORTING)
        archive = await Archive.create(generation=generation, relative_path="books.zip")
        author = await Author.create(generation=generation, name="Author", name_sort="author")
        genre = await Genre.create(
            generation=generation,
            code="fiction",
            label="Fiction",
            label_sort="fiction",
        )
        book = await Book.create(
            generation=generation,
            public_id="book-id",
            archive=archive,
            member_filename="book.fb2",
            title="Book",
            title_sort="book",
            size=123,
            original_format="fb2",
        )
        await BookAuthor.create(book=book, author=author, position=0)
        await BookGenre.create(book=book, genre=genre)

        await generation.delete()

        remaining = {
            model.Meta.table: await model.all().count()
            for model in (Archive, Author, Book, BookAuthor, BookGenre, Genre)
        }
    finally:
        await close_database(context)

    assert remaining == {table: 0 for table in remaining}


@pytest.mark.asyncio
async def test_initialize_database_exits_context_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingContext:
        exited = False

        def __enter__(self) -> FailingContext:
            return self

        async def init(self, **_: object) -> None:
            raise ValueError("init failed")

        async def close_connections(self) -> None:
            raise RuntimeError("close failed")

        def __exit__(
            self,
            exception_type: object,
            exception: object,
            traceback: object,
        ) -> None:
            self.exited = True

    failing_context = FailingContext()
    monkeypatch.setattr(database_connection, "TortoiseContext", lambda: failing_context)

    with pytest.raises(RuntimeError, match="close failed"):
        await initialize_database(tmp_path / "catalog.sqlite3")

    assert failing_context.exited


@pytest.mark.asyncio
async def test_close_database_exits_context_when_connection_close_fails() -> None:
    class FailingContext:
        exited = False

        async def close_connections(self) -> None:
            raise RuntimeError("close failed")

        def __exit__(
            self,
            exception_type: object,
            exception: object,
            traceback: object,
        ) -> None:
            self.exited = True

    failing_context = FailingContext()
    with pytest.raises(RuntimeError, match="close failed"):
        await close_database(cast(TortoiseContext, failing_context))

    assert failing_context.exited


@pytest.mark.asyncio
async def test_runtime_validation_rejects_pending_migrations(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM tortoise_migrations WHERE name = '0002_fts5'")

    with pytest.raises(PendingMigrationsError, match="0002_fts5"):
        await validate_migration_state(database_path)


@pytest.mark.asyncio
async def test_runtime_validation_rejects_missing_dependency_row(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM tortoise_migrations WHERE name = '0001_initial'")

    with pytest.raises(MigrationError, match="missing dependencies: 0001_initial"):
        await validate_migration_state(database_path)


@pytest.mark.asyncio
async def test_runtime_validation_rejects_unknown_migration_record(tmp_path: Path) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO tortoise_migrations(app, name, applied_at) "
            "VALUES ('catalog', '9999_future', CURRENT_TIMESTAMP)"
        )

    with pytest.raises(MigrationError, match="unknown or newer migrations: 9999_future"):
        await validate_migration_state(database_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("object_name", ["book", "book_fts"])
async def test_runtime_validation_rejects_dropped_required_schema_object(
    tmp_path: Path,
    object_name: str,
) -> None:
    database_path = tmp_path / "catalog.sqlite3"
    await apply_migrations(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(f'DROP TABLE "{object_name}"')

    with pytest.raises(MigrationError, match=object_name):
        await validate_migration_state(database_path)
