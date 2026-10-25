"""FastAPI application factory.

    uvicorn "nexus.main:create_app" --factory      # or: nexus serve

Startup order (lifespan): database → MCP engine (connectors, host client) →
LLM router → templates (registered as MCP prompts) → Streamable HTTP session
manager for external clients on ``/mcp``.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from nexus import __version__
from nexus.agent import Agent
from nexus.api import approvals, chat, connectors, llm, system, templates, ui
from nexus.config import Settings, get_settings
from nexus.llm_router import LLMRouter
from nexus.mcp_engine import MCPEngine
from nexus.providers import ProviderError
from nexus.templates import TemplateManager

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
log = logging.getLogger("nexus")


@dataclass
class Nexus:
    """Everything the routes need, hung off ``app.state.nexus``."""

    settings: Settings
    engine: MCPEngine
    router: LLMRouter
    agent: Agent
    templates: TemplateManager


def create_app(
    settings: Settings | None = None,
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(), format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    )
    engine = MCPEngine(settings, http_transport=http_transport)
    router = LLMRouter(settings, engine.db, engine.crypto, http_transport=llm_transport)
    nexus = Nexus(
        settings=settings,
        engine=engine,
        router=router,
        agent=Agent(engine, router, engine.db),
        templates=TemplateManager(engine, engine.db),
    )
    # Build the Streamable HTTP app now so its routes exist before startup.
    mcp_http = engine.server.streamable_http_app(streamable_http_path="/mcp", host=settings.host)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await engine.start()
        await router.start()
        await nexus.templates.load_all()
        async with engine.server.session_manager.run():
            log.info(
                "MCP Nexus %s ready on http://%s:%s (MCP endpoint: /mcp)",
                __version__,
                settings.host,
                settings.port,
            )
            try:
                yield
            finally:
                await router.stop()
                await engine.stop()

    app = FastAPI(
        title="MCP Nexus",
        version=__version__,
        description="Local MCP hub: multi-LLM chat, connectors, approvals, templates, inspector.",
        lifespan=lifespan,
    )
    app.state.nexus = nexus

    for module in (system, llm, connectors, approvals, templates, chat, ui):
        app.include_router(module.router)
    # Mount the MCP endpoint as plain routes (avoids the /mcp → /mcp/ redirect of a Mount).
    app.router.routes.extend(mcp_http.routes)

    @app.exception_handler(ProviderError)
    async def _provider_error(_: Request, exc: ProviderError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=502)

    @app.exception_handler(KeyError)
    async def _not_found(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse({"detail": f"Not found: {exc.args[0] if exc.args else exc}"}, 404)

    @app.exception_handler(ValueError)
    async def _bad_request(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=400)

    static_dir = WEB_DIR / "static"
    if static_dir.is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def index(request: Request) -> HTMLResponse:
        if (WEB_DIR / "templates" / "index.html").exists():
            return ui.pages.TemplateResponse(request, "index.html", {"version": __version__})
        return HTMLResponse(
            f"<h1>MCP Nexus {__version__}</h1><p>The dashboard arrives in Phase 4. "
            "Meanwhile see the <a href='/docs'>API docs</a>.</p>"
        )

    return app
