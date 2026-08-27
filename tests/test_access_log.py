"""Tests for query-safe Uvicorn access logging."""

import logging
from typing import cast

from sopds.access_log import QuerySafeAccessFormatter


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
