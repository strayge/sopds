"""Uvicorn access-log formatting that excludes URL query parameters."""

import logging
from copy import copy
from typing import Any, override

from uvicorn.logging import AccessFormatter


class QuerySafeAccessFormatter(AccessFormatter):
    """Preserve access-log paths while excluding query parameters."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        sanitized = copy(record)
        args: Any = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            copied = list(args)
            copied[2] = copied[2].split("?", 1)[0]
            sanitized.args = tuple(copied)
        return super().format(sanitized)
