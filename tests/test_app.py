"""Smoke tests for the initial HTTP application."""

import sqlite3
import time
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from tortoise.context import TortoiseContext

from sopds.app import create_app
from sopds.config import AppConfig
from sopds.db.connection import close_database


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
    row: tuple[str, str] | None = None
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        deadline = time.monotonic() + 2
        while row is None and time.monotonic() < deadline:
            with sqlite3.connect(migrated_app_config.database.path) as connection:
                row = connection.execute(
                    "SELECT state,error_summary FROM import_run ORDER BY id DESC LIMIT 1"
                ).fetchone()
            time.sleep(0.01)

    assert row == ("failed", "Could not read the configured catalog source")


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
