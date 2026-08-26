"""Command-line entry point for the single-process server."""

import argparse
import asyncio
from pathlib import Path

import uvicorn

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
    )


if __name__ == "__main__":
    main()
