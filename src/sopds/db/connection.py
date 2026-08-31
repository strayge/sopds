"""Runtime PostgreSQL connection lifecycle and live connection validation."""

import logging

import tortoise.context as tortoise_context
from tortoise.context import TortoiseContext, set_global_context

from sopds.config import DatabaseConfig
from sopds.db.configuration import (
    CONNECTION_NAME,
    POOL_MAX_SIZE,
    POOL_MIN_SIZE,
    build_tortoise_config,
)

_LOGGER = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    """Reports database startup failures without including configured contents."""


async def initialize_database(database: DatabaseConfig) -> TortoiseContext:
    """Open the shared pool and prove that PostgreSQL accepts application queries."""
    try:
        config = build_tortoise_config(database)
    except Exception:
        raise DatabaseError("Could not initialize PostgreSQL database") from None

    context = TortoiseContext()
    context.__enter__()
    try:
        await context.init(config=config)
        await _validate_postgresql_connection(context)
        _LOGGER.info(
            f"Database connection ready component=database backend=postgresql "
            f"pool_min_size={POOL_MIN_SIZE} pool_max_size={POOL_MAX_SIZE}"
        )
        set_global_context(context)
    except BaseException as error:
        cleanup_error: BaseException | None = None
        try:
            await context.close_connections()
        except BaseException as caught_cleanup_error:
            cleanup_error = caught_cleanup_error
        try:
            context.__exit__(None, None, None)
        except BaseException as caught_exit_error:
            if cleanup_error is None or isinstance(cleanup_error, Exception):
                cleanup_error = caught_exit_error

        if not isinstance(error, Exception):
            raise
        if cleanup_error is not None and not isinstance(cleanup_error, Exception):
            raise cleanup_error from error
        raise DatabaseError("Could not initialize PostgreSQL database") from error
    return context


async def close_database(context: TortoiseContext) -> None:
    """Remove ORM context state even when releasing connections is interrupted."""
    try:
        await context.close_connections()
    finally:
        try:
            context.__exit__(None, None, None)
        finally:
            # Tortoise 1.1.8 clears this fallback only after closing the pool succeeds.
            if tortoise_context._global_context is context:
                tortoise_context._global_context = None


async def _validate_postgresql_connection(context: TortoiseContext) -> None:
    """Fail startup unless a query succeeds through the runtime pool."""
    _, rows = await context.db(CONNECTION_NAME).execute_query("SELECT 1 AS connection_ok")
    if not rows or int(rows[0]["connection_ok"]) != 1:
        raise DatabaseError("Could not validate PostgreSQL connection")
