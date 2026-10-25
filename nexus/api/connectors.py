"""Connector Manager: connectors, their config, raw MCP tools/resources/prompts, webhook targets."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from nexus.api.deps import get_nexus

router = APIRouter(prefix="/api", tags=["connectors"])
TARGET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


def _mask_url(url: str) -> str:
    """Show only the host: webhook URLs carry their secret token in the path."""
    host = urlsplit(url).hostname or ""
    return f"{host}/…" if host else "…"


class ToolCallIn(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class TargetIn(BaseModel):
    kind: Literal["discord", "slack", "http"] = "http"
    url: str
    description: str = ""


# ── connectors ───────────────────────────────────────────────
@router.get("/connectors")
async def list_connectors(nexus=Depends(get_nexus)) -> list[dict[str, Any]]:
    return nexus.engine.registry.public()


@router.post("/connectors/reload")
async def reload_plugins(nexus=Depends(get_nexus)) -> list[dict[str, Any]]:
    await nexus.engine.registry.reload_plugins()
    return nexus.engine.registry.public()


@router.post("/connectors/{name}/enable")
async def enable(name: str, nexus=Depends(get_nexus)) -> dict[str, Any]:
    return (await nexus.engine.registry.set_enabled(name, True)).public()


@router.post("/connectors/{name}/disable")
async def disable(name: str, nexus=Depends(get_nexus)) -> dict[str, Any]:
    return (await nexus.engine.registry.set_enabled(name, False)).public()


@router.get("/connectors/{name}/config")
async def get_config(name: str, nexus=Depends(get_nexus)) -> dict[str, Any]:
    return nexus.engine.registry.config_view(name)


@router.put("/connectors/{name}/config")
async def put_config(name: str, values: dict[str, Any], nexus=Depends(get_nexus)) -> dict[str, Any]:
    from pydantic import ValidationError

    try:
        record = await nexus.engine.registry.update_config(name, values)
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False)) from None
    return record.public() | {"config": nexus.engine.registry.config_view(name)}


# ── raw MCP surface (via the tapped host client) ─────────────
@router.get("/tools")
async def list_tools(nexus=Depends(get_nexus)) -> list[dict[str, Any]]:
    return [
        {
            "name": t.name,
            "title": t.title,
            "description": t.description,
            "input_schema": t.input_schema,
            "annotations": t.annotations.model_dump(exclude_none=True) if t.annotations else {},
            "connector": (t.meta or {}).get("nexus/connector"),
            "requires_approval": bool((t.meta or {}).get("nexus/requiresApproval")),
        }
        for t in await nexus.engine.list_tools()
    ]


@router.post("/tools/{name}/call")
async def call_tool(name: str, body: ToolCallIn, nexus=Depends(get_nexus)) -> dict[str, Any]:
    result = await nexus.engine.call_tool(name, body.arguments)
    return {
        "is_error": bool(result.is_error),
        "content": [c.model_dump(exclude_none=True) for c in result.content],
        "structured_content": result.structured_content,
    }


@router.get("/resources")
async def list_resources(nexus=Depends(get_nexus)) -> list[dict[str, Any]]:
    return [
        r.model_dump(exclude_none=True, mode="json") for r in await nexus.engine.list_resources()
    ]


@router.get("/resources/read")
async def read_resource(uri: str, nexus=Depends(get_nexus)) -> dict[str, Any]:
    res = await nexus.engine.read_resource(uri)
    return {"contents": [c.model_dump(exclude_none=True, mode="json") for c in res.contents]}


@router.get("/prompts")
async def list_prompts(nexus=Depends(get_nexus)) -> list[dict[str, Any]]:
    return [p.model_dump(exclude_none=True) for p in await nexus.engine.list_prompts()]


# ── webhook targets ──────────────────────────────────────────
@router.get("/targets")
async def list_targets(nexus=Depends(get_nexus)) -> list[dict[str, Any]]:
    out = []
    s = nexus.settings
    for name, secret in (("discord", s.discord_webhook_url), ("slack", s.slack_webhook_url)):
        if secret and secret.get_secret_value():
            out.append(
                {
                    "name": name,
                    "kind": name,
                    "url_masked": _mask_url(secret.get_secret_value()),
                    "description": "from .env",
                    "source": "env",
                }
            )
    for row in await nexus.engine.db.list_webhook_targets():
        url = nexus.engine.crypto.decrypt(row["url_enc"]) or ""
        out.append(
            {
                "name": row["name"],
                "kind": row["kind"],
                "url_masked": _mask_url(url),
                "description": row["description"],
                "source": "ui",
            }
        )
    return out


@router.put("/targets/{name}")
async def put_target(name: str, body: TargetIn, nexus=Depends(get_nexus)) -> dict[str, str]:
    if not TARGET_NAME_RE.match(name):
        raise HTTPException(400, "Target names: lowercase letters, digits, - and _")
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")
    await nexus.engine.db.upsert_webhook_target(
        name,
        kind=body.kind,
        url_enc=nexus.engine.crypto.encrypt(body.url),
        description=body.description,
    )
    return {"name": name, "kind": body.kind}


@router.delete("/targets/{name}", status_code=204)
async def delete_target(name: str, nexus=Depends(get_nexus)) -> None:
    await nexus.engine.db.delete_webhook_target(name)
