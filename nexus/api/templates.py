"""Template Studio: CRUD, validation, preview, and run-through-the-agent."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse

from nexus.api.deps import get_nexus, sse
from nexus.templates import TemplateIn

router = APIRouter(prefix="/api/templates", tags=["templates"])


class Arguments(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


class RunIn(Arguments):
    model: str | None = None
    conversation_id: str | None = None


class BodyIn(BaseModel):
    body: str


@router.get("")
async def list_templates(nexus=Depends(get_nexus)) -> list[dict[str, Any]]:
    return await nexus.templates.list()


@router.post("/validate")
async def validate(body: BodyIn, nexus=Depends(get_nexus)) -> dict[str, Any]:
    try:
        return {"ok": True, "variables": nexus.templates.variables(body.body)}
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/{name}")
async def get_template(name: str, nexus=Depends(get_nexus)) -> dict[str, Any]:
    row = await nexus.templates.get(name)
    if row is None:
        raise HTTPException(404, f"No template {name!r}")
    return row


@router.put("/{name}")
async def save_template(name: str, body: TemplateIn, nexus=Depends(get_nexus)) -> dict[str, Any]:
    if body.name != name:
        raise HTTPException(400, "Name in path and body must match.")
    return await nexus.templates.save(body)


@router.delete("/{name}", status_code=204)
async def delete_template(name: str, nexus=Depends(get_nexus)) -> None:
    await nexus.templates.delete(name)


@router.post("/{name}/render")
async def render(name: str, body: Arguments, nexus=Depends(get_nexus)) -> dict[str, Any]:
    """Render through MCP ``prompts/get`` so the preview shows up in the Inspector too."""
    if await nexus.templates.get(name) is None:
        raise HTTPException(404, f"No template {name!r}")
    try:
        nexus.templates.render_row(await nexus.templates.get(name), body.arguments)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    result = await nexus.engine.get_prompt(name, {k: str(v) for k, v in body.arguments.items()})
    text = "\n\n".join(getattr(m.content, "text", "") for m in result.messages)
    return {"name": name, "text": text}


@router.post("/{name}/run")
async def run(name: str, body: RunIn, nexus=Depends(get_nexus)) -> EventSourceResponse:
    """Render the template and stream the agent's answer (same events as /api/chat)."""
    try:
        text = await nexus.templates.render(name, body.arguments)
    except KeyError:
        raise HTTPException(404, f"No template {name!r}") from None
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None

    async def stream():
        yield sse("prompt", {"text": text, "template": name})
        async for event in nexus.agent.run(
            text, conversation_id=body.conversation_id, model=body.model
        ):
            yield sse(event["type"], event)

    return EventSourceResponse(stream())
