"""Typed application configuration loaded exclusively from TOML."""

import tomllib
from pathlib import Path
from typing import Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    PositiveInt,
    SecretStr,
    ValidationError,
    model_validator,
)


class ConfigurationError(ValueError):
    """Prevents configuration failures from exposing secret input values."""


class ConfigModel(BaseModel):
    """Rejects unknown settings so configuration mistakes fail at startup."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ServerConfig(ConfigModel):
    host: str = "127.0.0.1"
    port: int = 8000
    base_url: AnyHttpUrl

    @model_validator(mode="after")
    def validate_public_base_url(self) -> Self:
        """Keep generated public links canonical and free of ambiguous URL components."""
        url = self.base_url
        if url.username is not None or url.password is not None:
            raise ValueError("base_url must not contain userinfo")
        if url.query is not None:
            raise ValueError("base_url must not contain a query")
        if url.fragment is not None:
            raise ValueError("base_url must not contain a fragment")
        return self


class CatalogConfig(ConfigModel):
    inpx_path: Path
    archive_root: Path
    check_interval_hours: PositiveInt = 12


class DatabaseConfig(ConfigModel):
    path: Path


class TelegramConfig(ConfigModel):
    enabled: bool = False
    token: SecretStr | None = None
    allowed_chat_ids: tuple[int, ...] = ()

    @model_validator(mode="after")
    def validate_enabled_bot(self) -> Self:
        """An enabled bot must not accidentally start without access controls."""
        token = self.token.get_secret_value() if self.token is not None else ""
        if self.enabled and not token:
            raise ValueError("token is required when Telegram is enabled")
        if self.enabled and not self.allowed_chat_ids:
            raise ValueError("allowed_chat_ids is required when Telegram is enabled")
        return self


class ConversionConfig(ConfigModel):
    cache_dir: Path
    cache_ttl_seconds: PositiveInt = 3600
    cleanup_interval_seconds: PositiveInt = 600


class AppConfig(ConfigModel):
    server: ServerConfig
    catalog: CatalogConfig
    database: DatabaseConfig
    telegram: TelegramConfig
    conversion: ConversionConfig


def load_config(path: Path) -> AppConfig:
    """Keep parsing errors useful without echoing potentially secret TOML values."""
    try:
        with path.open("rb") as config_file:
            raw_config = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationError(f"Could not read configuration {path}: {error}") from error

    try:
        return AppConfig.model_validate(raw_config)
    except ValidationError as error:
        messages = []
        for detail in error.errors(include_input=False):
            location = ".".join(str(part) for part in detail["loc"])
            messages.append(f"{location}: {detail['msg']}")
        raise ConfigurationError("Invalid configuration: " + "; ".join(messages)) from error
