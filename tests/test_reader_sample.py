"""Reader-sample database selection and failure-safety tests."""

from __future__ import annotations

from collections.abc import Callable
from subprocess import CompletedProcess
from typing import Any

import pytest
import scripts.check_reader_sample as reader_sample


class _FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _FakeConnection:
    def __init__(self, *, generation: int | None, rows: list[dict[str, object]]) -> None:
        self.generation = generation
        self.rows = rows
        self.transaction_options: dict[str, object] | None = None
        self.fetchval_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []
        self.closed = False

    def transaction(self, **kwargs: object) -> _FakeTransaction:
        self.transaction_options = kwargs
        return _FakeTransaction()

    async def fetchval(self, query: str, *args: object) -> object:
        self.fetchval_calls.append((query, args))
        if len(self.fetchval_calls) == 1:
            return self.generation
        return len(self.rows)

    async def fetch(self, query: str, *args: object) -> list[dict[str, object]]:
        self.fetch_calls.append((query, args))
        return self.rows

    async def close(self) -> None:
        self.closed = True


def _connect_factory(connection: _FakeConnection) -> Callable[[str], Any]:
    async def connect(database_url: str) -> _FakeConnection:
        assert database_url == "postgresql://sopds:database-secret@db.example/catalog"
        return connection

    return connect


def test_database_url_is_required_from_environment_without_echoing_a_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SOPDS_DATABASE_URL", raising=False)

    with pytest.raises(ValueError, match="SOPDS_DATABASE_URL") as error:
        reader_sample.database_url_from_environment()

    assert "database-secret" not in str(error.value)


def test_database_url_validation_and_errors_are_credential_safe() -> None:
    database_url = "postgresql://sopds:database-secret@db.example:5432/catalog?sslmode=require"

    assert reader_sample.validate_database_url(database_url) == database_url
    assert reader_sample.database_label(database_url) == "PostgreSQL at db.example:5432/catalog"

    with pytest.raises(ValueError) as error:
        reader_sample.validate_database_url("sqlite://sopds:database-secret@db.example/catalog")
    assert "database-secret" not in str(error.value)

    message = reader_sample.safe_database_error(
        RuntimeError(f"could not connect to {database_url} password=database-secret")
    )
    assert "database-secret" not in message
    assert "credentials omitted" in message


def test_playwright_process_does_not_inherit_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_environment: dict[str, str] = {}

    def run(*_args: object, **kwargs: object) -> CompletedProcess[str]:
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        captured_environment.update(environment)
        return CompletedProcess([], 0, "", "")

    monkeypatch.setenv(
        "SOPDS_DATABASE_URL", "postgresql://sopds:database-secret@db.example/catalog"
    )
    monkeypatch.setattr("scripts.check_reader_sample.subprocess.run", run)

    reader_sample.run_cli(["playwright-cli", "run-code", "noop"], timeout=1)

    assert "SOPDS_DATABASE_URL" not in captured_environment
    assert captured_environment["NO_COLOR"] == "1"


@pytest.mark.asyncio
async def test_collect_sample_uses_stable_read_only_snapshot_and_filters_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(
        generation=42,
        rows=[
            {"public_id": "reader-1", "source_format": "fb2"},
            {"public_id": "reader-2", "source_format": "epub"},
        ],
    )
    monkeypatch.setattr("scripts.check_reader_sample.asyncpg.connect", _connect_factory(connection))

    sample, observed_count, generation_id = await reader_sample.collect_sample(
        "postgresql://sopds:database-secret@db.example/catalog", 10
    )

    assert sample == [
        reader_sample.Candidate("reader-1", "fb2"),
        reader_sample.Candidate("reader-2", "epub"),
    ]
    assert observed_count == 2
    assert generation_id == 42
    assert connection.transaction_options == {"isolation": "repeatable_read", "readonly": True}
    assert connection.fetchval_calls[1][1] == (42,)
    sample_query, sample_args = connection.fetch_calls[0]
    assert sample_args == (42, 10)
    assert "b.hidden = FALSE" in sample_query
    assert "a.available = TRUE" in sample_query
    assert "ORDER BY random() LIMIT $2" in sample_query
    assert connection.closed


@pytest.mark.asyncio
async def test_collect_sample_closes_connection_when_catalog_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _FakeConnection(generation=None, rows=[])
    monkeypatch.setattr("scripts.check_reader_sample.asyncpg.connect", _connect_factory(connection))

    with pytest.raises(RuntimeError, match="no active generation"):
        await reader_sample.collect_sample(
            "postgresql://sopds:database-secret@db.example/catalog", 1
        )

    assert connection.closed
