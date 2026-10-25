"""Health, settings, the live event stream and the Inspector history."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sse_starlette import EventSourceResponse

from nexus import __version__
from nexus.api.deps import get_nexus, merged_subscription, sse

router = APIRouter(prefix="/api", tags=["system"])
TOPICS = {"rpc", "approvals", "connectors"}


@router.get("/health")
async def health(nexus=Depends(get_nexus)) -> dict[str, Any]:
    tools = await nexus.engine.list_tools()
    return {
        "status": "ok",
        "version": __version__,
        "tools": len(tools),
        "connectors": {r["name"]: r["status"] for r in nexus.engine.registry.public()},
        "active_model": await nexus.router.active_model(),
    }


@router.get("/settings")
async def settings_view(nexus=Depends(get_nexus)) -> dict[str, Any]:
    s = nexus.settings
    return {
        "version": __version__,
        "host": s.host,
        "port": s.port,
        "workspace_dir": str(s.workspace_dir.resolve()),
        "plugins_dir": str(s.plugins_dir.resolve()),
        "data_dir": str(s.data_dir.resolve()),
        "mcp_endpoint": f"http://{s.host}:{s.port}/mcp",
        "auto_approve_outbound": await nexus.engine.approvals.auto_approve(),
        "approval_timeout_seconds": s.approval_timeout_seconds,
        "active_model": await nexus.router.active_model(),
    }


@router.get("/events")
async def events(
    topics: Annotated[str, Query(description="Comma-separated: rpc, approvals, connectors")] = (
        "rpc,approvals,connectors"
    ),
    nexus=Depends(get_nexus),
) -> EventSourceResponse:
    """Server-sent events: live JSON-RPC traffic, approval requests, connector changes."""
    wanted = [t for t in (x.strip() for x in topics.split(",")) if t in TOPICS] or sorted(TOPICS)

    async def stream():
        async for topic, event in merged_subscription(nexus.engine.bus, wanted):
            yield sse(topic, event)

    return EventSourceResponse(stream(), ping=15)


@router.get("/inspector/history")
async def inspector_history(
    limit: Annotated[int, Query(ge=1, le=2000)] = 500, nexus=Depends(get_nexus)
) -> list[dict[str, Any]]:
    await nexus.engine.tap.flush()
    return await nexus.engine.db.list_rpc_entries(limit)


@router.delete("/inspector/history", status_code=204)
async def inspector_clear(nexus=Depends(get_nexus)) -> None:
    await nexus.engine.tap.flush()
    await nexus.engine.db.clear_rpc_log()
