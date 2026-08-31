"""PostgreSQL configuration, migration, and runtime connection tests."""

import asyncio
from pathlib import Path
from typing import cast

import asyncpg  # type: ignore[import-untyped]
import pytest
import tortoise.context as tortoise_context
from pydantic import SecretStr
from tortoise.config import ConnectionConfig
from tortoise.context import TortoiseContext, get_current_context

import sopds.db.connection as database_connection
import sopds.db.migrations as migrations_package
from sopds.config import DatabaseConfig
from sopds.db.configuration import POOL_MAX_SIZE, POOL_MIN_SIZE, build_tortoise_config
from sopds.db.connection import DatabaseError, close_database, initialize_database
from sopds.db.migrations_runner import (
    REQUIRED_FTS_INDEXES,
    REQUIRED_FTS_VECTORS,
    REQUIRED_SCHEMA_OBJECTS,
    MigrationError,
    apply_migrations,
    validate_migration_state,
)
from tests.conftest import _isolated_test_database_url, reset_test_database

MIGRATION_NAMES = tuple(
    sorted(path.stem for path in Path(migrations_package.__file__).parent.glob("[0-9]*.py"))
)


def _database_config(url: str = "postgresql://sopds@postgres:5432/sopds_test") -> DatabaseConfig:
    return DatabaseConfig(url=SecretStr(url))


def test_tortoise_config_uses_asyncpg_with_a_fixed_bounded_pool() -> None:
    config = build_tortoise_config(
        _database_config("postgresql://sopds@postgres:5432/sopds?min_size=9&max_size=20")
    )
    connection = cast(ConnectionConfig, config.connections["default"])

    assert connection.engine == "tortoise.backends.asyncpg"
    assert connection.db_url is None
    assert connection.credentials["minsize"] == POOL_MIN_SIZE == 1
    assert connection.credentials["maxsize"] == POOL_MAX_SIZE == 5
    assert "min_size" not in connection.credentials
    assert "max_size" not in connection.credentials


async def test_apply_migrations_hides_invalid_url_query_values() -> None:
    config = _database_config(
        "postgresql://sopds:database-secret@postgres:5432/sopds?min_size=query-secret"
    )

    with pytest.raises(MigrationError, match=r"^Could not apply database migrations$") as error:
        await apply_migrations(config)

    assert error.value.__cause__ is None


async def test_validate_migration_state_hides_invalid_url_query_values() -> None:
    config = _database_config(
        "postgresql://sopds:database-secret@postgres:5432/sopds?min_size=query-secret"
    )

    with pytest.raises(MigrationError, match=r"^Could not validate database migrations$") as error:
        await validate_migration_state(config)

    assert error.value.__cause__ is None


async def test_initialize_database_hides_invalid_url_query_values() -> None:
    config = _database_config(
        "postgresql://sopds:database-secret@postgres:5432/sopds?min_size=query-secret"
    )

    with pytest.raises(DatabaseError, match=r"^Could not initialize PostgreSQL database$") as error:
        await initialize_database(config)

    assert error.value.__cause__ is None


async def test_native_migrations_create_and_validate_postgresql_schema(
    test_database_url: str,
) -> None:
    config = _database_config(test_database_url)

    await apply_migrations(config)
    await apply_migrations(config)
    await validate_migration_state(config)

    connection = await asyncpg.connect(test_database_url)
    try:
        history = await connection.fetch("SELECT app, name FROM tortoise_migrations ORDER BY name")
        tables = {
            str(row["tablename"])
            for row in await connection.fetch(
                "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = current_schema()"
            )
        }
        vectors = {
            str(row["attname"])
            for row in await connection.fetch(
                "SELECT a.attname FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_catalog.pg_type t ON t.oid = a.atttypid "
                "WHERE n.nspname = current_schema() AND c.relname = 'book_fts' "
                "AND a.attgenerated = 's' AND t.typname = 'tsvector'"
            )
        }
        indexes = {
            str(row["indexname"])
            for row in await connection.fetch(
                "SELECT indexname FROM pg_catalog.pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = 'book_fts'"
            )
        }
        state = await connection.fetchrow(
            "SELECT id, active_generation_id FROM catalog_state WHERE id = 1"
        )
        transaction = connection.transaction()
        await transaction.start()
        try:
            await connection.execute(
                "INSERT INTO catalog_generation(id, state, created_at) "
                "VALUES (100, 'importing', CURRENT_TIMESTAMP)"
            )
            await connection.execute(
                "INSERT INTO archive(id, generation_id, relative_path, available) "
                "VALUES (100, 100, 'test.zip', TRUE)"
            )
            await connection.execute(
                "INSERT INTO book(id, generation_id, public_id, archive_id, member_filename, "
                "title, title_sort, size, original_format) "
                "VALUES (100, 100, 'test', 100, 'test.fb2', 'Test book', 'test book', 1, 'fb2')"
            )
            await connection.execute(
                "INSERT INTO book_fts(book_id, generation_id, title, authors, series, genres, language) "
                "VALUES (100, 100, 'test book', 'test author', '', 'fiction', 'en')"
            )
            generated_vector_matches = await connection.fetchval(
                "SELECT all_vector @@ plainto_tsquery('simple', 'author') "
                "FROM book_fts WHERE book_id = 100"
            )
        finally:
            await transaction.rollback()
    finally:
        await connection.close()

    assert [(row["app"], row["name"]) for row in history] == [
        ("catalog", name) for name in MIGRATION_NAMES
    ]
    assert tables >= REQUIRED_SCHEMA_OBJECTS | {"tortoise_migrations"}
    assert vectors == REQUIRED_FTS_VECTORS
    assert indexes >= REQUIRED_FTS_INDEXES
    assert generated_vector_matches is True
    assert state is not None
    assert (state["id"], state["active_generation_id"]) == (1, None)


async def test_runtime_connection_validates_postgresql(test_database_url: str) -> None:
    config = _database_config(test_database_url)
    await apply_migrations(config)

    context = await initialize_database(config)
    try:
        _, rows = await context.db().execute_query("SELECT current_database() AS name")
    finally:
        await close_database(context)

    assert len(rows) == 1
    assert rows[0]["name"]


async def test_initialize_database_does_not_install_failed_context_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_contexts: list[object] = []

    class FailingContext:
        exited = False

        def __enter__(self) -> FailingContext:
            return self

        async def init(self, **options: object) -> None:
            if options.get("_enable_global_fallback"):
                global_contexts.append(self)

        async def close_connections(self) -> None:
            raise RuntimeError("cleanup failed")

        def __exit__(
            self,
            exception_type: object,
            exception: object,
            traceback: object,
        ) -> None:
            self.exited = True

    async def fail_validation(_context: object) -> None:
        raise ValueError("validation failed")

    failing_context = FailingContext()
    monkeypatch.setattr(database_connection, "TortoiseContext", lambda: failing_context)
    monkeypatch.setattr(database_connection, "set_global_context", global_contexts.append)
    monkeypatch.setattr(database_connection, "_validate_postgresql_connection", fail_validation)

    with pytest.raises(DatabaseError):
        await initialize_database(_database_config())

    assert global_contexts == []
    assert failing_context.exited


async def test_initialize_database_installs_global_fallback_after_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class SuccessfulContext:
        def __enter__(self) -> SuccessfulContext:
            return self

        async def init(self, **_: object) -> None:
            events.append("init")

        def __exit__(
            self,
            exception_type: object,
            exception: object,
            traceback: object,
        ) -> None:
            return None

    async def validate(_context: object) -> None:
        events.append("validate")

    context = SuccessfulContext()
    monkeypatch.setattr(database_connection, "TortoiseContext", lambda: context)
    monkeypatch.setattr(
        database_connection, "set_global_context", lambda _context: events.append("global")
    )
    monkeypatch.setattr(database_connection, "_validate_postgresql_connection", validate)

    initialized = await initialize_database(_database_config())

    assert id(initialized) == id(context)
    assert events == ["init", "validate", "global"]


async def test_initialize_database_exits_context_and_hides_connection_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingContext:
        exited = False

        def __enter__(self) -> FailingContext:
            return self

        async def init(self, **_: object) -> None:
            raise ValueError("postgresql://sopds:database-secret@postgres/sopds")

        async def close_connections(self) -> None:
            return None

        def __exit__(
            self,
            exception_type: object,
            exception: object,
            traceback: object,
        ) -> None:
            self.exited = True

    failing_context = FailingContext()
    monkeypatch.setattr(database_connection, "TortoiseContext", lambda: failing_context)

    with pytest.raises(DatabaseError) as error:
        await initialize_database(_database_config())

    assert "database-secret" not in str(error.value)
    assert failing_context.exited


async def test_initialize_database_hides_cleanup_failure_and_exits_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingContext:
        exited = False

        def __enter__(self) -> FailingContext:
            return self

        async def init(self, **_: object) -> None:
            raise ValueError("initialization failed")

        async def close_connections(self) -> None:
            raise RuntimeError("cleanup-secret")

        def __exit__(
            self,
            exception_type: object,
            exception: object,
            traceback: object,
        ) -> None:
            self.exited = True

    failing_context = FailingContext()
    monkeypatch.setattr(database_connection, "TortoiseContext", lambda: failing_context)

    with pytest.raises(DatabaseError) as error:
        await initialize_database(_database_config())

    assert "cleanup-secret" not in str(error.value)
    assert isinstance(error.value.__cause__, ValueError)
    assert failing_context.exited


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql://sopds@postgres:5432/sopds",
        "postgresql://sopds@postgres:5432/catalog",
        "postgresql://sopds@postgres:5432/contest",
        "postgresql://sopds@postgres:5432/sopds_test?sslmode=disable",
        "postgresql://sopds@postgres:5432/sopds_test?options=-csearch_path%3Dprivate",
        "sqlite:///sopds_test",
    ],
)
def test_test_database_url_rejects_non_test_databases(database_url: str) -> None:
    with pytest.raises(ValueError):
        _isolated_test_database_url(database_url)


def test_test_database_url_accepts_explicit_test_database() -> None:
    database_url = "postgresql://sopds@postgres:5432/sopds_test"

    assert _isolated_test_database_url(database_url) == database_url


async def test_test_database_reset_rejects_non_public_current_schema(
    test_database_url: str,
) -> None:
    database = _database_config(f"{test_database_url}?options=-csearch_path%3Dpg_catalog")

    with pytest.raises(pytest.UsageError, match="public as the current schema"):
        await reset_test_database(database)


async def test_close_database_clears_global_fallback_on_close_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_error = RuntimeError("cleanup failed")

    class FailingContext:
        exited = False

        async def close_connections(self) -> None:
            raise cleanup_error

        def __exit__(self, *_: object) -> None:
            self.exited = True

    failing_context = FailingContext()
    context = cast(TortoiseContext, failing_context)
    monkeypatch.setattr(tortoise_context, "_global_context", context)

    with pytest.raises(RuntimeError) as error:
        await close_database(context)

    assert error.value is cleanup_error
    assert failing_context.exited
    assert get_current_context() is None


async def test_close_database_clears_global_fallback_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancellation = asyncio.CancelledError()

    class CancelledContext:
        exited = False

        async def close_connections(self) -> None:
            raise cancellation

        def __exit__(self, *_: object) -> None:
            self.exited = True

    cancelled_context = CancelledContext()
    context = cast(TortoiseContext, cancelled_context)
    monkeypatch.setattr(tortoise_context, "_global_context", context)

    with pytest.raises(asyncio.CancelledError) as error:
        await close_database(context)

    assert error.value is cancellation
    assert cancelled_context.exited
    assert get_current_context() is None
