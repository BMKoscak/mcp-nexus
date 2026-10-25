"""Server-rendered HTML fragments for HTMX (connector cards, conversation list)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from nexus.api.deps import get_nexus

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
pages = Jinja2Templates(directory=WEB_DIR / "templates")

router = APIRouter(prefix="/ui", tags=["ui"], include_in_schema=False)


def _render(request: Request, name: str, **ctx) -> HTMLResponse:
    return pages.TemplateResponse(request, name, ctx)


@router.get("/connectors", response_class=HTMLResponse)
async def connectors(request: Request, nexus=Depends(get_nexus)) -> HTMLResponse:
    return _render(request, "partials/connectors.html", connectors=nexus.engine.registry.public())


@router.post("/connectors/reload", response_class=HTMLResponse)
async def reload(request: Request, nexus=Depends(get_nexus)) -> HTMLResponse:
    await nexus.engine.registry.reload_plugins()
    return _render(request, "partials/connectors.html", connectors=nexus.engine.registry.public())


@router.post("/connectors/{name}/toggle", response_class=HTMLResponse)
async def toggle(name: str, request: Request, nexus=Depends(get_nexus)) -> HTMLResponse:
    record = nexus.engine.registry.records[name]
    record = await nexus.engine.registry.set_enabled(name, record.status != "active")
    return _render(request, "partials/connector_card.html", c=record.public())


@router.get("/conversations", response_class=HTMLResponse)
async def conversations(
    request: Request, active: str | None = None, nexus=Depends(get_nexus)
) -> HTMLResponse:
    rows = await nexus.engine.db.list_conversations()
    return _render(request, "partials/conversations.html", conversations=rows, active=active)
