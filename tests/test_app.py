"""Smoke tests for the initial HTTP application."""

import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock, patch
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from tortoise.context import TortoiseContext

from sopds.acquisition.archive import ArchiveService
from sopds.acquisition.zip_store import ZipOriginalStore
from sopds.app import create_app
from sopds.catalog.service import CatalogService
from sopds.config import AppConfig, TelegramConfig
from sopds.conversion.cache import ArtifactCache
from sopds.conversion.contracts import CacheCleanupSummary
from sopds.conversion.policy import OUTPUT_POLICY
from sopds.conversion.service import ConversionService
from sopds.db.connection import close_database
from sopds.imports.coordinator import ImportCoordinator
from sopds.lifecycle import _scheduled_checks, _scheduled_conversion_cleanup


def test_health_endpoint(migrated_app_config: AppConfig) -> None:
    with TestClient(create_app(migrated_app_config)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_endpoint_reports_database_failure_without_logging_details(
    migrated_app_config: AppConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    error_detail = "sensitive-connection-detail"
    with (
        patch.object(
            CatalogService,
            "check_readiness",
            autospec=True,
            side_effect=RuntimeError(error_detail),
        ),
        TestClient(create_app(migrated_app_config)) as client,
    ):
        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "RuntimeError" in caplog.text
    assert error_detail not in caplog.text


def test_health_is_unavailable_without_catalog_state_singleton(
    migrated_app_config: AppConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with TestClient(create_app(migrated_app_config)) as client:
        connection = sqlite3.connect(migrated_app_config.database.path)
        try:
            connection.execute("DELETE FROM catalog_state WHERE id = 1")
            connection.commit()
        finally:
            connection.close()

        response = client.get("/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert "RuntimeError" in caplog.text
    assert "Catalog database is not ready" not in caplog.text


def test_lifecycle_registers_pinned_converters_without_a_separate_conversion_api(
    migrated_app_config: AppConfig,
) -> None:
    app = create_app(migrated_app_config)
    with (
        patch(
            "sopds.conversion.process.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
        ) as execute,
        TestClient(app),
    ):
        registry = app.state.converter_registry
        epub = registry.resolve("fb2", "epub").converter
        fb2_azw3 = registry.resolve("fb2", "azw3").converter
        epub_azw3 = registry.resolve("epub", "azw3").converter
        assert len(registry) == 3
        assert epub.identity.name == "fb2cng"
        assert fb2_azw3.identity.name == "fb2cng-kindling"
        assert epub_azw3.identity.name == "kindling"
        assert epub._executable == "/usr/local/bin/fbc"
        assert fb2_azw3._fbc_executable == "/usr/local/bin/fbc"
        assert fb2_azw3._kindling_executable == "/usr/local/bin/kindling-cli"
        assert epub_azw3._executable == "/usr/local/bin/kindling-cli"
        execute.assert_not_awaited()
        assert not any(
            route.path.startswith("/conversion")
            for route in app.routes
            if isinstance(route, APIRoute)
        )


def test_index_uses_shared_server_rendered_shell(migrated_app_config: AppConfig) -> None:
    with TestClient(create_app(migrated_app_config)) as client:
        response = client.get("/")
        management = client.get("/manage")

    assert response.status_code == 200
    assert "INPX-backed catalog" in response.text
    assert 'href="#main-content">Skip to main content</a>' in response.text
    assert '<a href="/" aria-current="page">Catalog</a>' in response.text
    assert '<a href="/manage">Manage</a>' in response.text
    assert 'id="catalog-statistics"' not in response.text
    assert 'id="operation-status"' not in response.text
    assert management.status_code == 200
    assert '<a href="/manage" aria-current="page">Manage</a>' in management.text
    assert 'id="catalog-statistics"' in management.text
    assert 'id="operation-status"' in management.text
    assert "Application is healthy" in response.text
    assert "/health-fragment" not in response.text
    assert 'hx-trigger="load, every 30s"' not in response.text
    assert "function localizeCatalogTimes(root)" in response.text
    assert 'addEventListener("htmx:afterSwap"' in response.text
    assert "localizeCatalogTimes(event.detail.elt || event.target)" in response.text
    assert 'hourCycle: "h23"' in response.text
    assert "/static/css/app.css" in response.text
    assert "/static/vendor/htmx/htmx-2.0.10.min.js" in response.text
    assert "<style>" not in response.text
    assert "unpkg.com" not in response.text


def test_shared_stylesheet_and_fonts_are_served_locally(
    migrated_app_config: AppConfig,
) -> None:
    font_paths = (
        "/static/fonts/Literata-SemiBold.woff2",
        "/static/fonts/IBMPlexSans-Regular.woff2",
        "/static/fonts/IBMPlexSans-SemiBold.woff2",
        "/static/fonts/NotoSerif-SemiBold.woff2",
    )
    with TestClient(create_app(migrated_app_config)) as client:
        stylesheet = client.get("/static/css/app.css")
        fonts = [client.get(path) for path in font_paths]

    assert stylesheet.status_code == 200
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert stylesheet.text.count("font-display: swap") == 4
    assert all(path.rsplit("/", 1)[-1] in stylesheet.text for path in font_paths)
    assert '--font-result-title: "Noto Serif", Georgia, serif;' in stylesheet.text
    assert "IBM Plex Serif" not in stylesheet.text
    assert "font-family: var(--font-result-title);" in stylesheet.text
    assert "http://" not in stylesheet.text
    assert "https://" not in stylesheet.text
    assert all(response.status_code == 200 for response in fonts)
    assert all(response.headers["content-type"] == "font/woff2" for response in fonts)
    assert all(response.content.startswith(b"wOF2") for response in fonts)


def test_vendored_htmx_is_served_locally(migrated_app_config: AppConfig) -> None:
    with TestClient(create_app(migrated_app_config)) as client:
        response = client.get("/static/vendor/htmx/htmx-2.0.10.min.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert response.text.startswith("var htmx=")


def test_missing_inpx_stays_healthy_and_records_immediate_check(
    migrated_app_config: AppConfig,
) -> None:
    app = create_app(migrated_app_config)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        deadline = time.monotonic() + 2
        status = client.get("/imports/status")
        while "<dt>State</dt><dd>failed</dd>" not in status.text and time.monotonic() < deadline:
            time.sleep(0.01)
            status = client.get("/imports/status")

        assert status.status_code == 200
        assert "<dt>State</dt><dd>failed</dd>" in status.text
        assert (
            '<dt>Error</dt><dd class="error">Could not read the configured catalog source</dd>'
            in status.text
        )


def test_lifespan_logs_startup_failure_type_and_duration(
    migrated_app_config: AppConfig,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with (
        patch(
            "sopds.lifecycle.validate_migration_state",
            new_callable=AsyncMock,
            side_effect=RuntimeError("sensitive startup detail"),
        ),
        pytest.raises(RuntimeError, match="sensitive startup detail"),
        TestClient(create_app(migrated_app_config)),
    ):
        pass

    assert "Application startup failed phase=startup" in caplog.text
    assert "failure_type=RuntimeError" in caplog.text
    assert "duration_ms=" in caplog.text
    assert "sensitive startup detail" not in caplog.text


async def test_partial_cache_cleanup_failure_recovers_once(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    summaries = iter(
        [
            CacheCleanupSummary(1, 1),
            CacheCleanupSummary(0, 0),
        ]
    )

    class FakeCache:
        async def cleanup(self) -> CacheCleanupSummary:
            try:
                return next(summaries)
            except StopIteration:
                raise asyncio.CancelledError from None

    async def no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr("sopds.lifecycle.asyncio.sleep", no_wait)
    caplog.set_level("INFO", logger="sopds.lifecycle")
    with pytest.raises(asyncio.CancelledError):
        await _scheduled_conversion_cleanup(FakeCache(), 1)  # type: ignore[arg-type]

    assert caplog.text.count("cleanup failed entries") == 1
    assert caplog.text.count("cleanup recovered") == 1
    assert "failure_count=1" in caplog.text


def test_disabled_lifecycle_does_not_construct_telegram_bot(
    migrated_app_config: AppConfig,
) -> None:
    with (
        patch("sopds.lifecycle.TelegramRunner") as runner,
        TestClient(create_app(migrated_app_config)),
    ):
        pass

    runner.assert_not_called()


def test_telegram_starts_only_after_cache_startup_and_recovery(
    migrated_app_config: AppConfig,
) -> None:
    events: list[str] = []
    runner_args: tuple[object, ...] = ()
    telegram_config = TelegramConfig.model_validate(
        {"enabled": True, "token": "123456:secret", "allowed_chat_ids": [10]}
    )
    config = migrated_app_config.model_copy(update={"telegram": telegram_config})

    async def cache_startup(_cache: ArtifactCache) -> None:
        events.append("cache")

    async def recover(_coordinator: ImportCoordinator) -> None:
        events.append("recover")

    class FakeRunner:
        def __init__(self, *args: object) -> None:
            nonlocal runner_args
            runner_args = args
            events.append("constructed")

        def start(self) -> None:
            assert events == ["constructed", "cache", "recover"]
            events.append("started")

        async def shutdown(self) -> None:
            events.append("closed")

    app = create_app(config)
    with (
        patch.object(ArtifactCache, "startup", autospec=True, side_effect=cache_startup),
        patch.object(ImportCoordinator, "recover", autospec=True, side_effect=recover),
        patch("sopds.lifecycle.TelegramRunner", FakeRunner),
        TestClient(app),
    ):
        assert events == ["constructed", "cache", "recover", "started"]
        assert runner_args == (
            telegram_config,
            app.state.catalog,
            app.state.acquisition,
            app.state.conversion,
            OUTPUT_POLICY,
        )

    assert events[-1] == "closed"


def test_lifespan_initializes_and_closes_without_schema_generation(
    migrated_app_config: AppConfig,
) -> None:
    with (
        patch.object(TortoiseContext, "generate_schemas", new_callable=AsyncMock) as generate,
        patch(
            "sopds.lifecycle.close_database",
            new_callable=AsyncMock,
            wraps=close_database,
        ) as close,
        TestClient(create_app(migrated_app_config)) as client,
    ):
        assert client.get("/health").status_code == 200

    generate.assert_not_awaited()
    close.assert_awaited_once()


def test_lifespan_wires_archive_service_from_catalog_and_acquisition(
    migrated_app_config: AppConfig,
) -> None:
    app = create_app(migrated_app_config)
    with (
        patch("sopds.lifecycle.ArchiveService", autospec=ArchiveService) as archive_type,
        TestClient(app) as client,
    ):
        assert client.get("/selected").status_code == 200
        archive_type.assert_called_once_with(
            app.state.catalog,
            app.state.acquisition,
            app.state.conversion,
            OUTPUT_POLICY,
        )
        assert app.state.archive is archive_type.return_value


def test_lifespan_shuts_down_manual_work_before_database_close(
    migrated_app_config: AppConfig,
) -> None:
    shutdown_complete = False

    async def shutdown(_coordinator: ImportCoordinator) -> None:
        nonlocal shutdown_complete
        shutdown_complete = True

    async def close_after_shutdown(context: TortoiseContext) -> None:
        assert shutdown_complete
        await close_database(context)

    with (
        patch.object(ImportCoordinator, "shutdown", autospec=True, side_effect=shutdown),
        patch("sopds.lifecycle.close_database", autospec=True, side_effect=close_after_shutdown),
        TestClient(create_app(migrated_app_config)) as client,
    ):
        assert client.get("/health").status_code == 200

    assert shutdown_complete


def test_lifespan_runs_all_later_cleanup_when_coordinator_shutdown_fails(
    migrated_app_config: AppConfig,
) -> None:
    cleanup_order: list[str] = []
    conversion_shutdown = ConversionService.shutdown
    acquisition_shutdown = ZipOriginalStore.shutdown

    async def fail_coordinator_shutdown(_coordinator: ImportCoordinator) -> None:
        cleanup_order.append("coordinator")
        raise RuntimeError("shutdown failed")

    async def track_conversion_shutdown(service: ConversionService) -> None:
        cleanup_order.append("conversion")
        await conversion_shutdown(service)

    async def track_acquisition_shutdown(store: ZipOriginalStore) -> None:
        cleanup_order.append("acquisition")
        await acquisition_shutdown(store)

    async def track_database_close(context: TortoiseContext) -> None:
        cleanup_order.append("database")
        await close_database(context)

    with (
        pytest.raises(RuntimeError, match="shutdown failed"),
        patch.object(
            ImportCoordinator,
            "shutdown",
            autospec=True,
            side_effect=fail_coordinator_shutdown,
        ),
        patch.object(
            ConversionService,
            "shutdown",
            autospec=True,
            side_effect=track_conversion_shutdown,
        ),
        patch.object(
            ZipOriginalStore,
            "shutdown",
            autospec=True,
            side_effect=track_acquisition_shutdown,
        ),
        patch(
            "sopds.lifecycle.close_database",
            autospec=True,
            side_effect=track_database_close,
        ),
        TestClient(create_app(migrated_app_config)) as client,
    ):
        assert client.get("/health").status_code == 200

    assert cleanup_order == ["coordinator", "conversion", "acquisition", "database"]


def test_lifespan_closes_acquisition_before_database(
    migrated_app_config: AppConfig,
) -> None:
    acquisition_closed = False

    async def shutdown(_store: ZipOriginalStore) -> None:
        nonlocal acquisition_closed
        acquisition_closed = True

    async def close_after_acquisition(context: TortoiseContext) -> None:
        assert acquisition_closed
        await close_database(context)

    with (
        patch.object(ZipOriginalStore, "shutdown", autospec=True, side_effect=shutdown),
        patch("sopds.lifecycle.close_database", autospec=True, side_effect=close_after_acquisition),
        TestClient(create_app(migrated_app_config)) as client,
    ):
        assert client.get("/health").status_code == 200

    assert acquisition_closed


def test_scheduled_availability_refresh_updates_web_filters(
    migrated_app_config: AppConfig,
) -> None:
    fields = (
        "Author:",
        "sf:",
        "Facet Book",
        "",
        "",
        "book",
        "1",
        "",
        "0",
        "fb2",
        "2024-01-01",
        "zz",
        "",
        "",
    )
    with ZipFile(migrated_app_config.catalog.inpx_path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("nested/books.inp", "\x04".join(fields).encode() + b"\x04\r\n")

    async def fast_scheduled_checks(
        coordinator: ImportCoordinator,
        catalog: CatalogService,
        _interval_seconds: int,
    ) -> None:
        await _scheduled_checks(coordinator, catalog, 0.01)

    app = create_app(migrated_app_config)
    with (
        patch("sopds.lifecycle._scheduled_checks", new=fast_scheduled_checks),
        TestClient(app) as client,
    ):
        deadline = time.monotonic() + 3
        status = client.get("/imports/status")
        while "<dt>State</dt><dd>succeeded</dd>" not in status.text and time.monotonic() < deadline:
            time.sleep(0.01)
            status = client.get("/imports/status")
        assert status.status_code == 200
        assert "<dt>State</dt><dd>succeeded</dd>" in status.text

        cold = client.get("/")
        assert cold.status_code == 200
        assert 'value="zz"' not in cold.text

        available_archive = migrated_app_config.catalog.archive_root / "nested" / "books.zip"
        available_archive.parent.mkdir(parents=True)
        available_archive.touch()

        refreshed = cold
        deadline = time.monotonic() + 3
        while 'value="zz"' not in refreshed.text and time.monotonic() < deadline:
            time.sleep(0.01)
            refreshed = client.get("/")

        assert 'value="zz"' in refreshed.text


def test_create_app_uses_distinct_signing_keys_per_instance(app_config: AppConfig) -> None:
    first = create_app(app_config)
    second = create_app(app_config)

    assert first.state.csrf_key != second.state.csrf_key
    assert len(first.state.csrf_key) == 32
    assert first.state.cursor_key != second.state.cursor_key
    assert len(first.state.cursor_key) == 32
    assert first.state.cursor_key != first.state.csrf_key
