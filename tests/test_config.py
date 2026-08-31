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
url = "postgresql://sopds@postgres:5432/sopds"

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

    assert config.database.url.get_secret_value() == "postgresql://sopds@postgres:5432/sopds"
    assert config.telegram.enabled is True
    assert config.telegram.allowed_chat_ids == (123456789,)
    assert config.telegram.token is not None
    assert config.telegram.token.get_secret_value() == "test-secret-token"


@pytest.mark.parametrize(
    "base_url",
    [
        "http://user@example.test/catalog",
        "http://example.test/catalog?mode=opds",
        "http://example.test/catalog#feed",
    ],
)
def test_base_url_rejects_noncanonical_components(tmp_path: Path, base_url: str) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG.replace("http://localhost:8000", base_url))

    with pytest.raises(ConfigurationError, match="base_url"):
        load_config(config_path)


def test_base_url_accepts_path_prefix(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        VALID_CONFIG.replace("http://localhost:8000", "https://example.test/catalog")
    )

    assert str(load_config(config_path).server.base_url) == "https://example.test/catalog"


def test_database_accepts_passwordless_postgresql_url(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(VALID_CONFIG)

    assert load_config(config_path).database.url.get_secret_value().startswith("postgresql://")


@pytest.mark.parametrize("scheme", ["sqlite", "mysql"])
def test_database_rejects_other_schemes_without_leaking_url(tmp_path: Path, scheme: str) -> None:
    secret_url = f"{scheme}://sopds:database-secret@database.example/catalog"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        VALID_CONFIG.replace("postgresql://sopds@postgres:5432/sopds", secret_url)
    )

    with pytest.raises(ConfigurationError) as error:
        load_config(config_path)

    assert "database.url" in str(error.value)
    assert "database-secret" not in str(error.value)
    assert "database.example" not in str(error.value)


@pytest.mark.parametrize(
    "malformed_url",
    [
        "postgresql://user@host:notaport/catalog",
        "postgresql://@/catalog",
    ],
)
def test_database_rejects_malformed_authorities_without_leaking_url(
    tmp_path: Path,
    malformed_url: str,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        VALID_CONFIG.replace("postgresql://sopds@postgres:5432/sopds", malformed_url)
    )

    with pytest.raises(ConfigurationError) as error:
        load_config(config_path)

    assert "database.url" in str(error.value)
    assert malformed_url not in str(error.value)


def test_database_url_is_required(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        VALID_CONFIG.replace('url = "postgresql://sopds@postgres:5432/sopds"', "")
    )

    with pytest.raises(ConfigurationError, match=r"database\.url"):
        load_config(config_path)


def test_enabled_telegram_requires_allowlist_without_leaking_token(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        VALID_CONFIG.replace("allowed_chat_ids = [123456789]", "allowed_chat_ids = []")
    )

    with pytest.raises(ConfigurationError) as error:
        load_config(config_path)

    assert "allowed_chat_ids" in str(error.value)
    assert "test-secret-token" not in str(error.value)
