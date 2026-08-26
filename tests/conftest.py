"""Shared test configuration."""

import asyncio
from pathlib import Path

import pytest

from sopds.config import AppConfig
from sopds.db.migrations_runner import apply_migrations


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
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
            "database": {"path": tmp_path / "sopds.sqlite3"},
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
    asyncio.run(apply_migrations(app_config.database.path))
    return app_config
