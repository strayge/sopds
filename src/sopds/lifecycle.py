"""Application lifecycle coordination."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import UTC, datetime

from fastapi import FastAPI

from sopds.acquisition.service import AcquisitionService
from sopds.acquisition.zip_store import ZipOriginalStore
from sopds.catalog.service import CatalogService
from sopds.config import AppConfig
from sopds.conversion.cache import ArtifactCache
from sopds.conversion.registry import ConverterRegistry
from sopds.conversion.service import ConversionService
from sopds.db.connection import close_database, initialize_database
from sopds.db.migrations_runner import validate_migration_state
from sopds.db.repository import CatalogRepository
from sopds.imports.coordinator import ImportCoordinator
from sopds.telegram.runner import TelegramRunner

_LOGGER = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Supervise catalog work so no task or connection outlives database shutdown."""
    config: AppConfig = app.state.config
    await validate_migration_state(config.database.path)
    async with AsyncExitStack() as resources:
        database_context = await initialize_database(config.database.path)
        resources.push_async_callback(close_database, database_context)
        repository = CatalogRepository(database_context.db())
        coordinator = ImportCoordinator(
            repository,
            config.catalog.inpx_path,
            config.catalog.archive_root,
        )
        catalog = CatalogService(repository, app.state.cursor_key)
        acquisition = AcquisitionService(repository, ZipOriginalStore(config.catalog.archive_root))
        resources.push_async_callback(acquisition.shutdown)
        registry = ConverterRegistry()
        conversion_cache = ArtifactCache(
            config.conversion.cache_dir, config.conversion.cache_ttl_seconds
        )
        conversion = ConversionService(acquisition, registry, conversion_cache)
        resources.push_async_callback(conversion.shutdown)
        resources.push_async_callback(coordinator.shutdown)
        app.state.import_coordinator = coordinator
        app.state.catalog = catalog
        app.state.acquisition = acquisition
        app.state.conversion = conversion
        app.state.converter_registry = registry
        telegram: TelegramRunner | None = None
        if config.telegram.enabled:
            try:
                telegram = TelegramRunner(config.telegram, catalog, acquisition)
                resources.push_async_callback(telegram.shutdown)
            except Exception as error:
                _LOGGER.warning("Telegram initialization failed: %s", type(error).__name__)
        app.state.telegram = telegram

        await conversion_cache.startup()
        await coordinator.recover()
        if telegram is not None:
            try:
                telegram.start()
            except Exception as error:
                _LOGGER.warning("Telegram startup failed: %s", type(error).__name__)
        app.state.started_at = datetime.now(UTC)
        scheduler = asyncio.create_task(
            _scheduled_checks(
                coordinator,
                catalog,
                config.catalog.check_interval_hours * 3600,
            ),
            name="catalog-change-checker",
        )
        cleanup_scheduler = asyncio.create_task(
            _scheduled_conversion_cleanup(
                conversion_cache, config.conversion.cleanup_interval_seconds
            ),
            name="conversion-cache-cleaner",
        )
        resources.push_async_callback(_stop_schedulers, scheduler, cleanup_scheduler)
        yield


async def _stop_schedulers(*tasks: asyncio.Task[None]) -> None:
    """Cancel every scheduler and still observe all task completions when one fails."""
    for task in tasks:
        task.cancel()
    async with AsyncExitStack() as pending:
        for task in reversed(tasks):
            pending.push_async_callback(_await_cancelled_scheduler, task)


async def _await_cancelled_scheduler(task: asyncio.Task[None]) -> None:
    with suppress(asyncio.CancelledError):
        await task


async def _scheduled_conversion_cleanup(cache: ArtifactCache, interval_seconds: float) -> None:
    """Periodically expire cache files while isolating cleanup failures."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await cache.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Scheduled conversion cache cleanup failed")


async def _scheduled_checks(
    coordinator: ImportCoordinator,
    catalog: CatalogService,
    interval_seconds: float,
) -> None:
    """Run immediately, then isolate failures and wait before every retry."""
    while True:
        try:
            await coordinator.check_for_changes()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Scheduled catalog check failed")
        catalog.invalidate_filters()
        await asyncio.sleep(interval_seconds)
