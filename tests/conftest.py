"""Shared test configuration."""

import asyncio
import os
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import asyncpg  # type: ignore[import-untyped]
import pytest
from pydantic import SecretStr

from sopds.config import AppConfig, DatabaseConfig
from sopds.db.migrations_runner import apply_migrations


def _isolated_test_database_url(database_url: str) -> str:
    """Reject URLs whose database name does not clearly identify disposable test data."""
    try:
        parsed = urlsplit(database_url)
        database_name = unquote(parsed.path.removeprefix("/"))
        valid_port = parsed.port is None or parsed.port > 0
    except TypeError, ValueError:
        raise ValueError("invalid PostgreSQL test database URL") from None

    has_test_marker = re.search(r"(?:^|[_-])test(?:$|[_-])", database_name, flags=re.IGNORECASE)
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or not valid_port
        or not database_name
        or "/" in database_name
        or parsed.query
        or parsed.fragment
        or has_test_marker is None
    ):
        raise ValueError("URL must name a dedicated test database")
    return database_url


async def reset_test_database(database: DatabaseConfig) -> None:
    """Give each persistence test a clean PostgreSQL schema without shared ORM state."""
    connection = await asyncpg.connect(database.url.get_secret_value())
    try:
        current_schema = await connection.fetchval("SELECT current_schema()")
        if current_schema != "public":
            raise pytest.UsageError(
                "SOPDS_TEST_DATABASE_URL must connect with public as the current schema"
            )
        await connection.execute("DROP SCHEMA public CASCADE")
        await connection.execute("CREATE SCHEMA public")
    finally:
        await connection.close()


def isolated_database_config() -> DatabaseConfig:
    """Load the explicit disposable database URL for helpers that cannot use fixtures."""
    if os.environ.get("PYTEST_XDIST_WORKER") is not None:
        raise pytest.UsageError("PostgreSQL persistence tests must run in one pytest process")
    database_url = os.environ.get("SOPDS_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("SOPDS_TEST_DATABASE_URL is required for persistence tests")
    try:
        return DatabaseConfig(url=SecretStr(_isolated_test_database_url(database_url)))
    except ValueError:
        raise pytest.UsageError(
            "SOPDS_TEST_DATABASE_URL must name a dedicated test database (for example, sopds_test)"
        ) from None


@pytest.fixture
def test_database_url() -> str:
    """Reset and return an unmistakably isolated PostgreSQL test database."""
    database = isolated_database_config()
    asyncio.run(reset_test_database(database))
    return database.url.get_secret_value()


@pytest.fixture
def app_config(tmp_path: Path, test_database_url: str) -> AppConfig:
    return AppConfig.model_validate(
        {
            "server": {
                "host": "127.0.0.1",
                "port": 8000,
                "base_url": "http://testserver",
            },
            "catalog": {
                "inpx_path": tmp_path / "local.inpx",
                "archive_root": tmp_path,
                "check_interval_hours": 12,
            },
            "database": {"url": test_database_url},
            "telegram": {
                "enabled": False,
                "token": "",
                "allowed_chat_ids": [],
            },
            "conversion": {
                "cache_dir": tmp_path / "conversion-cache",
                "cache_ttl_seconds": 3600,
                "cleanup_interval_seconds": 600,
            },
        }
    )


@pytest.fixture
def migrated_app_config(app_config: AppConfig) -> AppConfig:
    """Mirror production by applying migrations before application lifespan."""
    asyncio.run(apply_migrations(app_config.database))
    return app_config
