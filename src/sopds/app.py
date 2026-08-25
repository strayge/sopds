"""FastAPI application composition root."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from sopds.config import AppConfig
from sopds.lifecycle import lifespan
from sopds.web.routes import router as web_router

_STATIC_DIRECTORY = Path(__file__).parent / "web" / "static"


def create_app(config: AppConfig) -> FastAPI:
    """Keep adapter wiring in one place as feature modules are introduced."""
    app = FastAPI(title="SOPDS", version="0.1.0", lifespan=lifespan)
    app.state.config = config
    app.mount("/static", StaticFiles(directory=_STATIC_DIRECTORY), name="static")
    app.include_router(web_router)
    return app
