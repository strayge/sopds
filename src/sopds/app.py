"""FastAPI application composition root."""

import secrets
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sopds.config import AppConfig
from sopds.lifecycle import lifespan
from sopds.opds.routes import router as opds_router
from sopds.web.routes import router as web_router

_STATIC_DIRECTORY = Path(__file__).parent / "web" / "static"


def create_app(config: AppConfig) -> FastAPI:
    """Keep adapter wiring in one place as feature modules are introduced."""
    app = FastAPI(title="SOPDS", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.cursor_key = secrets.token_bytes(32)
    app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")
    app.include_router(opds_router)
    app.include_router(web_router)
    return app
