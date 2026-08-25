"""Application lifecycle coordination."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Provide one future home for supervised bot, import, and cleanup tasks."""
    app.state.started_at = datetime.now(UTC)
    yield
