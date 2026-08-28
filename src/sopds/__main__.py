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

from sopds.access_log import AccessLogMiddleware, configure_access_logging
from sopds.app import create_app
from sopds.config import ConfigurationError, load_config
from sopds.db.connection import DatabaseError
from sopds.db.migrations_runner import MigrationError, apply_migrations
from sopds.reloader import run_reload_supervisor

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
    parser.add_argument(
        "--reload",
        action="store_true",
        help="restart the development server when Python or configuration files change",
    )
    return parser


def _logging_config() -> dict[str, Any]:
    config = deepcopy(LOGGING_CONFIG)
    configure_access_logging(config)
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
    if arguments.reload:
        logging.config.dictConfig(_logging_config())
        run_reload_supervisor(arguments.config, Path(__file__).resolve().parent)
        return

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

    app = create_app(config)
    asgi_app = AccessLogMiddleware(app)
    uvicorn.run(
        asgi_app,
        host=config.server.host,
        port=config.server.port,
        workers=1,
        log_config=logging_config,
        access_log=False,
    )


if __name__ == "__main__":
    main()
