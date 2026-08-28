"""Application and validation of committed native Tortoise migrations."""

from pathlib import Path

from tortoise.context import TortoiseContext
from tortoise.migrations.api import migrate
from tortoise.migrations.executor import MigrationExecutor

from sopds.db.configuration import APP_LABEL, CONNECTION_NAME, build_tortoise_config
from sopds.db.connection import ensure_database_parent

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


class MigrationError(RuntimeError):
    """Base error for migration startup failures."""


class PendingMigrationsError(MigrationError):
    """Prevents runtime startup against an older database schema."""


async def apply_migrations(database_path: Path) -> None:
    """Apply all committed migrations and always discard migration context state."""
    ensure_database_parent(database_path)
    try:
        async with TortoiseContext():
            await migrate(config=build_tortoise_config(database_path), app_labels=[APP_LABEL])
    except Exception as error:
        raise MigrationError(f"Could not apply database migrations: {error}") from error


async def validate_migration_state(database_path: Path) -> None:
    """Require the exact migration ledger and the schema objects runtime depends on."""
    config = build_tortoise_config(database_path)
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
        raise MigrationError(f"Could not validate database migrations: {error}") from error

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
            _, rows = await context.db(CONNECTION_NAME).execute_query(
                "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view')"
            )
    except Exception as error:
        raise MigrationError(f"Could not validate database schema objects: {error}") from error

    tables = {str(row["name"]) for row in rows if row["type"] == "table"}
    missing_objects = REQUIRED_SCHEMA_OBJECTS - tables
    if missing_objects:
        names = ", ".join(sorted(missing_objects))
        raise MigrationError(f"Database is missing required schema objects: {names}")
