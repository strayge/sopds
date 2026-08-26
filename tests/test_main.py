"""Command-line startup ordering tests."""

import sys
from pathlib import Path

import pytest

from sopds import __main__
from sopds.config import AppConfig


def test_cli_applies_migrations_before_uvicorn(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    uvicorn_arguments: dict[str, object] = {}

    async def fake_apply_migrations(database_path: Path) -> None:
        assert database_path == app_config.database.path
        events.append("migrate")

    def fake_uvicorn_run(*args: object, **kwargs: object) -> None:
        del args
        uvicorn_arguments.update(kwargs)
        events.append("uvicorn")

    monkeypatch.setattr(__main__, "load_config", lambda _path: app_config)
    monkeypatch.setattr(__main__, "apply_migrations", fake_apply_migrations)
    monkeypatch.setattr("sopds.__main__.uvicorn.run", fake_uvicorn_run)
    monkeypatch.setattr(sys, "argv", ["sopds", "--config", "unused.toml"])

    __main__.main()

    assert events == ["migrate", "uvicorn"]
    log_config = uvicorn_arguments["log_config"]
    assert isinstance(log_config, dict)
    loggers = log_config["loggers"]
    assert isinstance(loggers, dict)
    assert loggers["sopds"] == {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }
