"""FastAPI application composition root."""

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import override

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from sopds.config import AppConfig
from sopds.lifecycle import lifespan
from sopds.opds.routes import router as opds_router
from sopds.web.csrf import CSRF_KEY_BYTES
from sopds.web.i18n import compile_catalogs_if_needed
from sopds.web.routes import router as web_router

_STATIC_DIRECTORY = Path(__file__).parent / "web" / "static"


@asynccontextmanager
async def _application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Prepare Web catalogs before the service lifecycle can report readiness."""
    compile_catalogs_if_needed()
    async with lifespan(app):
        yield


class _RevalidatingStaticFiles(StaticFiles):
    """Prevent browsers from retaining stale executable and stylesheet assets."""

    @override
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if Path(path).suffix.lower() in {".css", ".js"}:
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(config: AppConfig) -> FastAPI:
    """Keep adapter wiring in one place as feature modules are introduced."""
    app = FastAPI(title="SOPDS", version="0.1.0", lifespan=_application_lifespan)
    app.state.config = config
    app.state.csrf_key = secrets.token_bytes(CSRF_KEY_BYTES)
    app.state.cursor_key = secrets.token_bytes(32)
    app.mount("/static", _RevalidatingStaticFiles(directory=_STATIC_DIRECTORY), name="static")
    app.include_router(opds_router)
    app.include_router(web_router)
    return app
