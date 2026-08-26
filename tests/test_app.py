"""Smoke tests for the initial HTTP application."""

import time
from unittest.mock import AsyncMock, patch
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi.testclient import TestClient
from tortoise.context import TortoiseContext

from sopds.app import create_app
from sopds.catalog.service import CatalogService
from sopds.config import AppConfig
from sopds.db.connection import close_database
from sopds.imports.coordinator import ImportCoordinator
from sopds.lifecycle import _scheduled_checks


def test_health_endpoint(migrated_app_config: AppConfig) -> None:
    with TestClient(create_app(migrated_app_config)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_index_uses_server_rendered_template(migrated_app_config: AppConfig) -> None:
    with TestClient(create_app(migrated_app_config)) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "INPX-backed catalog" in response.text
    assert 'hx-get="/health-fragment"' in response.text
    assert "/static/vendor/htmx/htmx-2.0.10.min.js" in response.text
    assert "unpkg.com" not in response.text


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


def test_create_app_uses_a_distinct_csrf_token_per_instance(app_config: AppConfig) -> None:
    first = create_app(app_config)
    second = create_app(app_config)

    assert first.state.csrf_token != second.state.csrf_token
    assert len(first.state.csrf_token) >= 32
    assert first.state.cursor_key != second.state.cursor_key
    assert len(first.state.cursor_key) == 32
    assert first.state.cursor_key != first.state.csrf_token
