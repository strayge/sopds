"""Initial web and health routes."""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


@router.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    return templates.TemplateResponse(request=request, name="index.html")


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse()


@router.get("/health-fragment", response_class=HTMLResponse)
async def health_fragment() -> HTMLResponse:
    return HTMLResponse('<span class="status-ok">Application is healthy</span>')
