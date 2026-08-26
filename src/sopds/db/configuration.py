"""Pure construction of the shared Tortoise configuration."""

from pathlib import Path

from tortoise.config import AppConfig as TortoiseAppConfig
from tortoise.config import ConnectionConfig, TortoiseConfig

SQLITE_BUSY_TIMEOUT_MS = 5_000
APP_LABEL = "catalog"
CONNECTION_NAME = "default"


def build_tortoise_config(database_path: Path) -> TortoiseConfig:
    """Keep every database phase on identical SQLite connection settings."""
    return TortoiseConfig(
        connections={
            CONNECTION_NAME: ConnectionConfig(
                engine="tortoise.backends.sqlite",
                credentials={
                    "file_path": str(database_path),
                    "journal_mode": "WAL",
                    "foreign_keys": "ON",
                    "busy_timeout": SQLITE_BUSY_TIMEOUT_MS,
                },
            )
        },
        apps={
            APP_LABEL: TortoiseAppConfig(
                models=["sopds.db.models"],
                default_connection=CONNECTION_NAME,
                migrations="sopds.db.migrations",
            )
        },
        use_tz=True,
        timezone="UTC",
    )
