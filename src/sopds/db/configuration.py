"""Pure construction of the shared PostgreSQL Tortoise configuration."""

from typing import Any, cast

from tortoise.backends.base.config_generator import expand_db_url
from tortoise.config import AppConfig as TortoiseAppConfig
from tortoise.config import ConnectionConfig, TortoiseConfig

from sopds.config import DatabaseConfig

APP_LABEL = "catalog"
CONNECTION_NAME = "default"
POOL_MIN_SIZE = 1
POOL_MAX_SIZE = 5


def build_tortoise_config(database: DatabaseConfig) -> TortoiseConfig:
    """Pin the shared asyncpg pool below PostgreSQL's deployment connection limit."""
    expanded: dict[str, Any] = expand_db_url(database.url.get_secret_value())
    credentials = dict(cast(dict[str, Any], expanded["credentials"]))
    credentials.pop("min_size", None)
    credentials.pop("max_size", None)
    credentials["minsize"] = POOL_MIN_SIZE
    credentials["maxsize"] = POOL_MAX_SIZE
    return TortoiseConfig(
        connections={
            CONNECTION_NAME: ConnectionConfig(
                engine="tortoise.backends.asyncpg",
                credentials=credentials,
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
