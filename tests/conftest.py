"""Shared test configuration."""

from pathlib import Path

import pytest

from sopds.config import AppConfig


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
