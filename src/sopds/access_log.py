"""Query-safe, completion-aware HTTP access logging."""

import logging
from copy import copy
from time import perf_counter_ns
from typing import Any, cast, override
from urllib.parse import quote

from starlette.types import ASGIApp, Message, Receive, Scope, Send
from uvicorn.logging import AccessFormatter

_LOGGER = logging.getLogger("sopds.access")


def configure_access_logging(config: dict[str, Any]) -> None:
    """Keep access-log implementation choices out of application bootstrap wiring."""
    formatters = cast(dict[str, Any], config["formatters"])
    formatters["access"]["()"] = "sopds.access_log.QuerySafeAccessFormatter"
    formatters["access"]["fmt"] += " %(duration_ms)dms"
    loggers = cast(dict[str, Any], config["loggers"])
    loggers["sopds.access"] = {
        "handlers": ["access"],
        "level": "INFO",
        "propagate": False,
    }


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


class AccessLogMiddleware:
    """Measure through the final body chunk so streamed transfers are represented."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = perf_counter_ns()
        status_code = 500
        response_logged = False
        trailers_expected = False

        def log_response() -> None:
            nonlocal response_logged
            if response_logged:
                return
            response_logged = True
            duration_ms = (perf_counter_ns() - started_at + 500_000) // 1_000_000
            client = scope.get("client")
            client_address = f"{client[0]}:{client[1]}" if client else ""
            raw_path = scope.get("raw_path")
            path = (
                raw_path.decode("ascii", errors="replace")
                if isinstance(raw_path, bytes)
                else quote(scope.get("path", ""))
            )
            _LOGGER.info(
                '%s - "%s %s HTTP/%s" %d',
                client_address,
                scope.get("method", ""),
                path,
                scope.get("http_version", ""),
                status_code,
                extra={"duration_ms": duration_ms},
            )

        async def observed_send(message: Message) -> None:
            nonlocal status_code, trailers_expected
            message_type = message["type"]
            if message_type == "http.response.start":
                status_code = message["status"]
                trailers_expected = message.get("trailers", False)
            await send(message)
            if message_type == "http.response.body":
                if not message.get("more_body", False) and not trailers_expected:
                    log_response()
            elif message_type == "http.response.trailers" and not message.get(
                "more_trailers", False
            ):
                log_response()

        try:
            await self.app(scope, receive, observed_send)
        finally:
            log_response()
