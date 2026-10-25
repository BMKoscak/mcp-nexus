"""Chat & Execution: conversations and the streaming agent endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sse_starlette import EventSourceResponse

from nexus.api.deps import get_nexus, sse

router = APIRouter(prefix="/api", tags=["chat"])


class ChatIn(BaseModel):
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    model: str | None = Field(None, description="provider/model; defaults to the active model")
    use_tools: bool = True
    temperature: float | None = Field(None, ge=0, le=2)


class ConversationIn(BaseModel):
    title: str = "New chat"
    model: str | None = None


@router.post("/chat")
async def chat(body: ChatIn, nexus=Depends(get_nexus)) -> EventSourceResponse:
    """Stream the agent's reply as SSE events (see ``nexus.agent`` for the event types)."""

    async def stream():
        async for event in nexus.agent.run(
            body.message,
            conversation_id=body.conversation_id,
            model=body.model,
            use_tools=body.use_tools,
            temperature=body.temperature,
        ):
            yield sse(event["type"], event)

    return EventSourceResponse(stream())


@router.get("/conversations")
async def list_conversations(nexus=Depends(get_nexus)) -> list[dict[str, Any]]:
    return await nexus.engine.db.list_conversations()


@router.post("/conversations", status_code=201)
async def create_conversation(body: ConversationIn, nexus=Depends(get_nexus)) -> dict[str, Any]:
    cid = await nexus.engine.db.create_conversation(body.title, model=body.model)
    return await nexus.engine.db.get_conversation(cid)


@router.get("/conversations/{cid}")
async def get_conversation(cid: str, nexus=Depends(get_nexus)) -> dict[str, Any]:
    convo = await nexus.engine.db.get_conversation(cid)
    if convo is None:
        raise HTTPException(404, "No such conversation")
    return convo | {"messages": await nexus.engine.db.list_messages(cid)}


@router.delete("/conversations/{cid}", status_code=204)
async def delete_conversation(cid: str, nexus=Depends(get_nexus)) -> None:
    await nexus.engine.db.delete_conversation(cid)
