"""Runtime database connection lifecycle and SQLite invariant checks."""

from pathlib import Path

from tortoise.context import TortoiseContext

from sopds.db.configuration import CONNECTION_NAME, SQLITE_BUSY_TIMEOUT_MS, build_tortoise_config


class DatabaseError(RuntimeError):
    """Reports database startup failures without including configured contents."""


def ensure_database_parent(database_path: Path) -> None:
    """Create only the configured database parent and preserve actionable OS errors."""
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise DatabaseError(f"Could not create database directory: {error}") from error


async def initialize_database(database_path: Path) -> TortoiseContext:
    """Open a migrated database and verify connection-local SQLite safeguards."""
    ensure_database_parent(database_path)
    context = TortoiseContext()
    context.__enter__()
    try:
        await context.init(
            config=build_tortoise_config(database_path),
            _enable_global_fallback=True,
        )
        await _validate_sqlite_pragmas(context)
    except BaseException:
        try:
            await context.close_connections()
        finally:
            context.__exit__(None, None, None)
        raise
    return context


async def close_database(context: TortoiseContext) -> None:
    """Remove ORM context state even when releasing connections is interrupted."""
    try:
        await context.close_connections()
    finally:
        context.__exit__(None, None, None)


async def _validate_sqlite_pragmas(context: TortoiseContext) -> None:
    """Validate WAL, foreign keys, and busy timeout where they take effect.

    These connection-local settings must be checked on the actual Tortoise connection, not
    inferred from another connection or the database file.
    """
    connection = context.db(CONNECTION_NAME)
    _, rows = await connection.execute_query(
        "SELECT "
        "(SELECT journal_mode FROM pragma_journal_mode) AS journal_mode, "
        "(SELECT foreign_keys FROM pragma_foreign_keys) AS foreign_keys, "
        "(SELECT timeout FROM pragma_busy_timeout) AS busy_timeout"
    )
    if not rows:
        raise DatabaseError("Could not validate SQLite connection settings")
    row = rows[0]
    if (
        str(row["journal_mode"]).lower() != "wal"
        or int(row["foreign_keys"]) != 1
        or int(row["busy_timeout"]) != SQLITE_BUSY_TIMEOUT_MS
    ):
        raise DatabaseError("SQLite connection settings are not safely configured")
