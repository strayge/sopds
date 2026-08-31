"""Application and validation of committed PostgreSQL Tortoise migrations."""

from tortoise.context import TortoiseContext
from tortoise.migrations.api import migrate
from tortoise.migrations.executor import MigrationExecutor

from sopds.config import DatabaseConfig
from sopds.db.configuration import APP_LABEL, CONNECTION_NAME, build_tortoise_config

REQUIRED_SCHEMA_OBJECTS = frozenset(
    {
        "archive",
        "archive_language",
        "archive_original_format",
        "archive_genre",
        "author",
        "book",
        "book_author",
        "book_fts",
        "book_genre",
        "catalog_generation",
        "catalog_source",
        "catalog_state",
        "genre",
        "import_run",
        "series",
    }
)
REQUIRED_FTS_VECTORS = frozenset({"all_vector", "title_vector", "authors_vector", "series_vector"})
REQUIRED_FTS_INDEXES = frozenset(
    {
        "book_fts_all_vector_idx",
        "book_fts_title_vector_idx",
        "book_fts_authors_vector_idx",
        "book_fts_series_vector_idx",
        "book_fts_generation_idx",
    }
)


class MigrationError(RuntimeError):
    """Base error for migration startup failures."""


class PendingMigrationsError(MigrationError):
    """Prevents runtime startup against an older database schema."""


async def apply_migrations(database: DatabaseConfig) -> None:
    """Apply all committed migrations without exposing connection details on failure."""
    try:
        config = build_tortoise_config(database)
    except Exception:
        raise MigrationError("Could not apply database migrations") from None

    try:
        async with TortoiseContext():
            await migrate(config=config, app_labels=[APP_LABEL])
    except Exception as error:
        raise MigrationError("Could not apply database migrations") from error


async def validate_migration_state(database: DatabaseConfig) -> None:
    """Require the exact migration ledger and PostgreSQL objects used at runtime."""
    try:
        config = build_tortoise_config(database)
    except Exception:
        raise MigrationError("Could not validate database migrations") from None

    try:
        async with TortoiseContext() as context:
            await context.init(config=config, init_connections=False)
            app_config = config.to_dict()["apps"][APP_LABEL]
            executor = MigrationExecutor(
                context.db(CONNECTION_NAME),
                {APP_LABEL: app_config},
            )
            await executor.loader.build_graph()
            disk_registry = {
                key.name: frozenset(
                    dependency_name
                    for dependency_app, dependency_name in migration.dependencies
                    if dependency_app == APP_LABEL
                )
                for key, migration in executor.loader.disk_migrations.items()
                if key.app_label == APP_LABEL
            }
            applied_names = {
                key.name for key in executor.loader.applied_migrations if key.app_label == APP_LABEL
            }
    except Exception as error:
        raise MigrationError("Could not validate database migrations") from error

    expected_names = disk_registry.keys()
    unknown = applied_names - expected_names
    if unknown:
        names = ", ".join(sorted(unknown))
        raise MigrationError(f"Database records unknown or newer migrations: {names}")

    dependency_holes = {
        dependency
        for name in applied_names
        for dependency in disk_registry[name]
        if dependency not in applied_names
    }
    if dependency_holes:
        names = ", ".join(sorted(dependency_holes))
        raise MigrationError(f"Database migration ledger has missing dependencies: {names}")

    pending = expected_names - applied_names
    if pending:
        names = ", ".join(sorted(pending))
        raise PendingMigrationsError(f"Database has unapplied migrations: {names}")

    try:
        async with TortoiseContext() as context:
            await context.init(config=config, init_connections=False)
            connection = context.db(CONNECTION_NAME)
            _, relation_rows = await connection.execute_query(
                "SELECT c.relname AS name "
                "FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relkind = 'r'"
            )
            _, vector_rows = await connection.execute_query(
                "SELECT a.attname AS name, a.attgenerated::text AS generated, t.typname AS type "
                "FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_catalog.pg_type t ON t.oid = a.atttypid "
                "WHERE n.nspname = current_schema() AND c.relname = 'book_fts' "
                "AND a.attnum > 0 AND NOT a.attisdropped"
            )
            _, index_rows = await connection.execute_query(
                "SELECT indexname AS name FROM pg_catalog.pg_indexes "
                "WHERE schemaname = current_schema() AND tablename = 'book_fts'"
            )
            _, state_rows = await connection.execute_query(
                "SELECT id, active_generation_id FROM catalog_state WHERE id = 1"
            )
    except Exception as error:
        raise MigrationError("Could not validate PostgreSQL database schema") from error

    tables = {str(row["name"]) for row in relation_rows}
    missing_objects = REQUIRED_SCHEMA_OBJECTS - tables
    if missing_objects:
        names = ", ".join(sorted(missing_objects))
        raise MigrationError(f"Database is missing required schema objects: {names}")

    generated_vectors = {
        str(row["name"])
        for row in vector_rows
        if row["generated"] == "s" and row["type"] == "tsvector"
    }
    missing_vectors = REQUIRED_FTS_VECTORS - generated_vectors
    if missing_vectors:
        names = ", ".join(sorted(missing_vectors))
        raise MigrationError(f"Database is missing required generated search vectors: {names}")

    indexes = {str(row["name"]) for row in index_rows}
    missing_indexes = REQUIRED_FTS_INDEXES - indexes
    if missing_indexes:
        names = ", ".join(sorted(missing_indexes))
        raise MigrationError(f"Database is missing required search indexes: {names}")

    if len(state_rows) != 1 or int(state_rows[0]["id"]) != 1:
        raise MigrationError("Database is missing the canonical catalog state")
