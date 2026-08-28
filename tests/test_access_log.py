"""Tests for query-safe, completion-aware access logging."""

import logging
from copy import deepcopy
from typing import cast

import pytest
from starlette.types import Message, Receive, Scope, Send
from uvicorn.config import LOGGING_CONFIG

from sopds.access_log import (
    AccessLogMiddleware,
    QuerySafeAccessFormatter,
    configure_access_logging,
)


def test_access_logging_configuration_owns_its_formatter_and_logger_details() -> None:
    config = deepcopy(LOGGING_CONFIG)

    configure_access_logging(config)

    assert config["formatters"]["access"]["()"] == "sopds.access_log.QuerySafeAccessFormatter"
    assert config["formatters"]["access"]["fmt"].endswith(" %(duration_ms)dms")
    assert config["loggers"]["sopds.access"] == {
        "handlers": ["access"],
        "level": "INFO",
        "propagate": False,
    }


def _access_record(target: str) -> logging.LogRecord:
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", target, "1.1", 200),
        None,
    )


@pytest.mark.asyncio
async def test_access_middleware_logs_rounded_duration_after_final_body(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = iter((1_000_000_000, 1_012_600_000))
    monkeypatch.setattr("sopds.access_log.perf_counter_ns", lambda: next(times))
    sent: list[Message] = []

    async def app(_scope: Scope, _receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        assert not [record for record in caplog.records if record.name == "sopds.access"]
        await send({"type": "http.response.body", "body": b"last"})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope = cast(
        Scope,
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/catalog/encoded path",
            "raw_path": b"/catalog/encoded%20path",
            "query_string": b"q=private",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "state": {},
        },
    )

    with caplog.at_level(logging.INFO, logger="sopds.access"):
        await AccessLogMiddleware(app)(scope, receive, send)

    records = [record for record in caplog.records if record.name == "sopds.access"]
    assert len(records) == 1
    assert records[0].__dict__["duration_ms"] == 13
    formatter = QuerySafeAccessFormatter(
        fmt='%(client_addr)s - "%(request_line)s" %(status_code)s %(duration_ms)dms'
    )
    rendered = formatter.format(records[0])
    assert rendered == '127.0.0.1:1234 - "GET /catalog/encoded%20path HTTP/1.1" 200 OK 13ms'
    assert b"".join(message.get("body", b"") for message in sent) == b"firstlast"


def test_access_formatter_removes_query() -> None:
    formatter = QuerySafeAccessFormatter(fmt='%(client_addr)s - "%(request_line)s" %(status_code)s')

    rendered = formatter.format(_access_record("/catalog?q=private%20search&cursor=secret"))

    assert '"GET /catalog HTTP/1.1"' in rendered
    assert "private" not in rendered
    assert "secret" not in rendered
    assert "?" not in rendered


def test_access_formatter_preserves_encoded_path() -> None:
    formatter = QuerySafeAccessFormatter(fmt='%(client_addr)s - "%(request_line)s" %(status_code)s')

    rendered = formatter.format(_access_record("/catalog/encoded%20path?q=private"))

    assert '"GET /catalog/encoded%20path HTTP/1.1"' in rendered


def test_access_formatter_does_not_modify_original_record() -> None:
    target = "/catalog?q=private"
    record = _access_record(target)
    formatter = QuerySafeAccessFormatter(fmt='%(client_addr)s - "%(request_line)s" %(status_code)s')

    formatter.format(record)

    assert cast(tuple[object, ...], record.args)[2] == target
