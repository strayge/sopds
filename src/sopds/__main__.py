"""Command-line entry point for the single-process server."""

import argparse
import asyncio
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from sopds.app import create_app
from sopds.config import ConfigurationError, load_config
from sopds.db.connection import DatabaseError
from sopds.db.migrations_runner import MigrationError, apply_migrations


def build_parser() -> argparse.ArgumentParser:
    """Require an explicit file while retaining a convenient local default."""
    parser = argparse.ArgumentParser(description="Run the SOPDS server")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.toml"),
        help="path to the application TOML file",
    )
    return parser


def _logging_config() -> dict[str, Any]:
    config = deepcopy(LOGGING_CONFIG)
    loggers = cast(dict[str, Any], config["loggers"])
    loggers["sopds"] = {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }
    return config


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()
    try:
        config = load_config(arguments.config)
    except ConfigurationError as error:
        parser.error(str(error))

    try:
        asyncio.run(apply_migrations(config.database.path))
    except (DatabaseError, MigrationError) as error:
        parser.error(str(error))

    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        workers=1,
        log_config=_logging_config(),
    )


if __name__ == "__main__":
    main()
