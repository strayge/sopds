"""Command-line entry point for the single-process server."""

import argparse
import asyncio
import logging
import logging.config
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import Any, cast

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from sopds.app import create_app
from sopds.config import ConfigurationError, load_config
from sopds.db.connection import DatabaseError
from sopds.db.migrations_runner import MigrationError, apply_migrations

_LOGGER = logging.getLogger(__name__)


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
    formatters = cast(dict[str, Any], config["formatters"])
    formatters["access"]["()"] = "sopds.access_log.QuerySafeAccessFormatter"
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

    logging_config = _logging_config()
    logging.config.dictConfig(logging_config)
    migration_started = perf_counter()
    _LOGGER.info("Database migration check started phase=migration")
    try:
        asyncio.run(apply_migrations(config.database.path))
    except DatabaseError, MigrationError:
        duration_ms = int((perf_counter() - migration_started) * 1000)
        _LOGGER.error(
            f"Database migration check failed phase=migration "
            f"failure_type=startup_database duration_ms={duration_ms}"
        )
        parser.error("Database migration failed")
    duration_ms = int((perf_counter() - migration_started) * 1000)
    _LOGGER.info(f"Database migration check completed phase=migration duration_ms={duration_ms}")

    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        workers=1,
        log_config=logging_config,
    )


if __name__ == "__main__":
    main()
