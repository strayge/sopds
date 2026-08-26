"""Application lifecycle coordination."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI

from sopds.config import AppConfig
from sopds.db.connection import close_database, initialize_database
from sopds.db.migrations_runner import validate_migration_state
from sopds.db.repository import CatalogRepository
from sopds.imports.coordinator import ImportCoordinator

_LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Supervise catalog work so no task or connection outlives database shutdown."""
    config: AppConfig = app.state.config
    await validate_migration_state(config.database.path)
    database_context = await initialize_database(config.database.path)
    coordinator = ImportCoordinator(
        CatalogRepository(database_context.db()),
        config.catalog.inpx_path,
        config.catalog.archive_root,
    )
    app.state.import_coordinator = coordinator
    scheduler: asyncio.Task[None] | None = None
    try:
        await coordinator.recover()
        app.state.started_at = datetime.now(UTC)
        scheduler = asyncio.create_task(
            _scheduled_checks(coordinator, config.catalog.check_interval_hours * 3600),
            name="catalog-change-checker",
        )
        yield
    finally:
        if scheduler is not None:
            scheduler.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler
        await close_database(database_context)


async def _scheduled_checks(coordinator: ImportCoordinator, interval_seconds: int) -> None:
    """Run immediately, then isolate failures and wait before every retry."""
    while True:
        try:
            await coordinator.check_for_changes()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Scheduled catalog check failed")
        await asyncio.sleep(interval_seconds)
