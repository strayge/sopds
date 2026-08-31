"""Command-line startup ordering tests."""

import sys
from pathlib import Path

import pytest

from sopds import __main__
from sopds.config import AppConfig, DatabaseConfig


def test_cli_reload_uses_supervisor_without_starting_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "config.toml"
    supervisor_arguments: tuple[Path, Path] | None = None

    def fake_run_reload_supervisor(config: Path, package: Path) -> None:
        nonlocal supervisor_arguments
        supervisor_arguments = config, package

    def unexpected_load_config(_path: Path) -> AppConfig:
        pytest.fail("reload parent must not initialize the application")

    monkeypatch.setattr(__main__, "run_reload_supervisor", fake_run_reload_supervisor)
    monkeypatch.setattr(__main__, "load_config", unexpected_load_config)
    monkeypatch.setattr(sys, "argv", ["sopds", "--config", str(config_path), "--reload"])

    __main__.main()

    assert supervisor_arguments is not None
    assert supervisor_arguments[0] == config_path
    assert supervisor_arguments[1].name == "sopds"


def test_cli_applies_migrations_before_uvicorn(
    app_config: AppConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    uvicorn_arguments: dict[str, object] = {}

    async def fake_apply_migrations(database: DatabaseConfig) -> None:
        assert database is app_config.database
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
    formatters = log_config["formatters"]
    assert isinstance(formatters, dict)
    assert formatters["default"]["()"] == "uvicorn.logging.DefaultFormatter"
    loggers = log_config["loggers"]
    assert isinstance(loggers, dict)
    assert loggers["sopds"] == {
        "handlers": ["default"],
        "level": "INFO",
        "propagate": False,
    }
    assert uvicorn_arguments["access_log"] is False
