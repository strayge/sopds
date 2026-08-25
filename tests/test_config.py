"""Configuration loading and secret-safe validation tests."""

from pathlib import Path

import pytest

from sopds.config import ConfigurationError, load_config

VALID_CONFIG = """
[server]
host = "127.0.0.1"
port = 8000
base_url = "http://localhost:8000"

[catalog]
inpx_path = "/library/local.inpx"
archive_root = "/library"
check_interval_hours = 12

[database]
path = "/data/sopds.sqlite3"

[telegram]
enabled = true
token = "test-secret-token"
allowed_chat_ids = [123456789]

[conversion]
cache_dir = "/data/conversion-cache"
cache_ttl_seconds = 3600
cleanup_interval_seconds = 600
"""


def test_load_config_reads_the_single_toml_file(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG)

    config = load_config(config_path)

    assert config.telegram.enabled is True
    assert config.telegram.allowed_chat_ids == (123456789,)
    assert config.telegram.token is not None
    assert config.telegram.token.get_secret_value() == "test-secret-token"


def test_enabled_telegram_requires_allowlist_without_leaking_token(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        VALID_CONFIG.replace("allowed_chat_ids = [123456789]", "allowed_chat_ids = []")
    )

    with pytest.raises(ConfigurationError) as error:
        load_config(config_path)

    assert "allowed_chat_ids" in str(error.value)
    assert "test-secret-token" not in str(error.value)
