"""Application lifecycle coordination."""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from datetime import UTC, datetime
from time import perf_counter

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
    startup_started = perf_counter()
    shutdown_started: float | None = None
    startup_ready = False
    _LOGGER.info("Application startup started phase=startup")
    try:
        await validate_migration_state(config.database.path)
        async with AsyncExitStack() as resources:
            database_context = await initialize_database(config.database.path)
            resources.push_async_callback(
                _observed_cleanup, "database", close_database, database_context
            )
            repository = CatalogRepository(database_context.db())
            coordinator = ImportCoordinator(
                repository,
                config.catalog.inpx_path,
                config.catalog.archive_root,
            )
            catalog = CatalogService(repository, app.state.cursor_key)
            acquisition = AcquisitionService(
                repository, ZipOriginalStore(config.catalog.archive_root)
            )
            resources.push_async_callback(_observed_cleanup, "acquisition", acquisition.shutdown)
            registry = ConverterRegistry()
            conversion_cache = ArtifactCache(
                config.conversion.cache_dir, config.conversion.cache_ttl_seconds
            )
            conversion = ConversionService(acquisition, registry, conversion_cache)
            resources.push_async_callback(_observed_cleanup, "conversion", conversion.shutdown)
            resources.push_async_callback(_observed_cleanup, "imports", coordinator.shutdown)
            app.state.import_coordinator = coordinator
            app.state.catalog = catalog
            app.state.acquisition = acquisition
            app.state.conversion = conversion
            app.state.converter_registry = registry
            telegram: TelegramRunner | None = None
            if config.telegram.enabled:
                try:
                    telegram = TelegramRunner(config.telegram, catalog, acquisition)
                    resources.push_async_callback(_observed_cleanup, "telegram", telegram.shutdown)
                except Exception as error:
                    _LOGGER.warning(
                        f"Telegram initialization failed phase=initialization "
                        f"component=telegram failure_type={type(error).__name__}"
                    )
            app.state.telegram = telegram

            await conversion_cache.startup()
            await coordinator.recover()
            if telegram is not None:
                try:
                    telegram.start()
                except Exception as error:
                    _LOGGER.warning(
                        f"Telegram startup failed phase=startup component=telegram "
                        f"failure_type={type(error).__name__}"
                    )
            app.state.started_at = datetime.now(UTC)
            scheduler = asyncio.create_task(
                _scheduled_checks(
                    coordinator,
                    catalog,
                    config.catalog.check_interval_hours * 3600,
                ),
                name="catalog-change-checker",
            )
            resources.push_async_callback(
                _observed_cleanup, "catalog_scheduler", _stop_schedulers, scheduler
            )
            cleanup_scheduler = asyncio.create_task(
                _scheduled_conversion_cleanup(
                    conversion_cache, config.conversion.cleanup_interval_seconds
                ),
                name="conversion-cache-cleaner",
            )
            resources.push_async_callback(
                _observed_cleanup,
                "conversion_cleanup_scheduler",
                _stop_schedulers,
                cleanup_scheduler,
            )
            duration_ms = int((perf_counter() - startup_started) * 1000)
            _LOGGER.info(
                f"Application startup ready phase=startup duration_ms={duration_ms} "
                f"converter_count={len(registry)}"
            )
            startup_ready = True
            try:
                yield
            finally:
                shutdown_started = perf_counter()
                _LOGGER.info("Application shutdown started phase=shutdown")
    except BaseException as error:
        if not startup_ready:
            duration_ms = int((perf_counter() - startup_started) * 1000)
            _LOGGER.error(
                f"Application startup failed phase=startup "
                f"failure_type={type(error).__name__} duration_ms={duration_ms}"
            )
        elif shutdown_started is not None:
            duration_ms = int((perf_counter() - shutdown_started) * 1000)
            _LOGGER.error(
                f"Application shutdown failed phase=shutdown "
                f"failure_type={type(error).__name__} duration_ms={duration_ms}"
            )
        raise
    else:
        if shutdown_started is not None:
            duration_ms = int((perf_counter() - shutdown_started) * 1000)
            _LOGGER.info(f"Application shutdown completed phase=shutdown duration_ms={duration_ms}")


async def _observed_cleanup(
    component: str, callback: Callable[..., Awaitable[object]], *args: object
) -> None:
    """Add component identity while leaving AsyncExitStack's LIFO/error behavior intact."""
    try:
        await callback(*args)
    except BaseException as error:
        _LOGGER.error(
            f"Application component shutdown failed phase=shutdown component={component} "
            f"failure_type={type(error).__name__}"
        )
        raise


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
    """Report transitions while keeping routine empty cleanup silent."""
    failures = 0
    while True:
        await asyncio.sleep(interval_seconds)
        started = perf_counter()
        try:
            summary = await cache.cleanup()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failures += 1
            duration_ms = int((perf_counter() - started) * 1000)
            _LOGGER.warning(
                f"Scheduled conversion cache cleanup failed phase=conversion_cleanup "
                f"failure_type={type(error).__name__} attempt={failures} "
                f"duration_ms={duration_ms}"
            )
            continue
        duration_ms = int((perf_counter() - started) * 1000)
        if summary.removed_files:
            _LOGGER.info(
                f"Scheduled conversion cache cleanup removed files "
                f"phase=conversion_cleanup removed_files={summary.removed_files} "
                f"duration_ms={duration_ms}"
            )
        if summary.failed_entries:
            failures += 1
            _LOGGER.warning(
                f"Scheduled conversion cache cleanup failed entries "
                f"phase=conversion_cleanup failed_entries={summary.failed_entries} "
                f"attempt={failures} duration_ms={duration_ms}"
            )
        elif failures:
            _LOGGER.info(
                f"Scheduled conversion cache cleanup recovered phase=conversion_cleanup "
                f"failure_count={failures} duration_ms={duration_ms}"
            )
            failures = 0


async def _scheduled_checks(
    coordinator: ImportCoordinator,
    catalog: CatalogService,
    interval_seconds: float,
) -> None:
    """Run immediately and report only failure/recovery transitions."""
    failures = 0
    while True:
        started = perf_counter()
        try:
            await coordinator.check_for_changes()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failures += 1
            duration_ms = int((perf_counter() - started) * 1000)
            _LOGGER.warning(
                f"Scheduled catalog check failed phase=catalog_check "
                f"failure_type={type(error).__name__} attempt={failures} "
                f"duration_ms={duration_ms}"
            )
        else:
            if failures:
                duration_ms = int((perf_counter() - started) * 1000)
                _LOGGER.info(
                    f"Scheduled catalog check recovered phase=catalog_check "
                    f"failure_count={failures} duration_ms={duration_ms}"
                )
                failures = 0
        catalog.invalidate_filters()
        await asyncio.sleep(interval_seconds)
