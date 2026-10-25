"""The chat agent: LLM ⇄ MCP tool-calling loop.

Every tool call goes through ``MCPEngine.call_tool`` (the tapped MCP client), so
the Inspector shows each step and approval-gated tools pause the loop until the
user decides. The loop yields plain dict events that the API streams as SSE:

    {"type": "conversation", "id"}            first event
    {"type": "text", "delta"}                 streamed assistant text
    {"type": "tool_call", "id", "name", "arguments"}
    {"type": "tool_result", "id", "name", "is_error", "content"}
    {"type": "notice", "message"}             non-fatal info (e.g. tools disabled)
    {"type": "error", "message"}
    {"type": "done", "stop_reason", "usage", "steps"}
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from nexus.db import Database
from nexus.llm_router import LLMRouter
from nexus.mcp_engine import MCPEngine
from nexus.providers import (
    Done,
    Message,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolDef,
)

log = logging.getLogger("nexus.agent")

DEFAULT_SYSTEM_PROMPT = (
    "You are MCP Nexus, a helpful assistant running on the user's own computer. "
    "You can call tools exposed by local MCP connectors: files in a sandboxed workspace, "
    "web pages, system stats, read-only SQLite, and outbound messages. Prefer using tools "
    "over guessing. Outbound tools (webhooks, Discord, Slack, email, notifications) may be "
    "held for the user's approval; if one is denied, tell the user and do not retry it."
)
MAX_TOOL_RESULT_CHARS = 20_000
NO_TOOLS_HINTS = ("does not support tools", "tool use is not supported", "tools are not supported")


class Agent:
    def __init__(
        self,
        engine: MCPEngine,
        router: LLMRouter,
        db: Database,
        *,
        max_steps: int = 8,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        self.engine = engine
        self.router = router
        self.db = db
        self.max_steps = max_steps
        self.system_prompt = system_prompt

    async def tool_defs(self) -> list[ToolDef]:
        return [
            ToolDef(t.name, t.description or t.name, t.input_schema or {"type": "object"})
            for t in await self.engine.list_tools()
        ]

    async def run(
        self,
        message: str,
        *,
        conversation_id: str | None = None,
        model: str | None = None,
        use_tools: bool = True,
        temperature: float | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        try:
            provider, model_name, model_ref = await self.router.resolve_model(model)
        except ProviderError as exc:
            yield {"type": "error", "message": str(exc)}
            return

        if conversation_id is None or await self.db.get_conversation(conversation_id) is None:
            conversation_id = await self.db.create_conversation(_title(message), model=model_ref)
        else:
            convo = await self.db.get_conversation(conversation_id)
            if convo and convo["title"] == "New chat":
                await self.db.update_conversation(conversation_id, title=_title(message))
            await self.db.update_conversation(conversation_id, model=model_ref)
        yield {"type": "conversation", "id": conversation_id, "model": model_ref}

        await self.db.add_message(conversation_id, "user", message)
        history = await self._history(conversation_id)
        tools = await self.tool_defs() if use_tools else []
        usage_total: dict[str, int] = {}
        stop_reason: str | None = None

        for step in range(1, self.max_steps + 1):
            text = ""
            calls: list[ToolCall] = []
            try:
                async for ev in provider.stream(
                    model_name,
                    history,
                    tools or None,
                    system=self.system_prompt,
                    temperature=temperature,
                ):
                    if isinstance(ev, TextDelta):
                        text += ev.text
                        yield {"type": "text", "delta": ev.text}
                    elif isinstance(ev, ToolCallEvent):
                        calls.append(ev.call)
                    elif isinstance(ev, Done):
                        stop_reason = ev.stop_reason
                        for k, v in ev.usage.items():
                            usage_total[k] = usage_total.get(k, 0) + (v or 0)
            except ProviderError as exc:
                if tools and any(h in str(exc).lower() for h in NO_TOOLS_HINTS):
                    tools = []
                    yield {
                        "type": "notice",
                        "message": f"{model_ref} does not support tool calling; "
                        "continuing without tools.",
                    }
                    continue
                yield {"type": "error", "message": str(exc)}
                return
            except Exception as exc:  # network errors etc.
                log.exception("LLM stream failed")
                yield {"type": "error", "message": f"{exc.__class__.__name__}: {exc}"}
                return

            await self.db.add_message(
                conversation_id,
                "assistant",
                text,
                tool_calls=[c.to_dict() for c in calls] or None,
            )
            history.append(Message("assistant", text, tool_calls=calls))
            if not calls:
                yield {
                    "type": "done",
                    "stop_reason": stop_reason,
                    "usage": usage_total,
                    "steps": step,
                }
                return

            for call in calls:
                yield {"type": "tool_call", **call.to_dict()}
                content, is_error = await self._call_tool(call)
                yield {
                    "type": "tool_result",
                    "id": call.id,
                    "name": call.name,
                    "is_error": is_error,
                    "content": content,
                }
                await self.db.add_message(
                    conversation_id, "tool", content, tool_call_id=call.id, name=call.name
                )
                history.append(
                    Message(
                        "tool", content, tool_call_id=call.id, name=call.name, is_error=is_error
                    )
                )

        yield {
            "type": "error",
            "message": f"Stopped after {self.max_steps} tool-calling steps without a final answer.",
        }

    async def _call_tool(self, call: ToolCall) -> tuple[str, bool]:
        try:
            result = await self.engine.call_tool(call.name, call.arguments)
        except Exception as exc:
            return f"Tool call failed: {exc}", True
        parts: list[str] = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
            else:
                parts.append(f"[{getattr(block, 'type', 'content')} omitted]")
        content = "\n".join(parts) or (
            json.dumps(result.structured_content) if result.structured_content else ""
        )
        if len(content) > MAX_TOOL_RESULT_CHARS:
            content = content[:MAX_TOOL_RESULT_CHARS] + "\n…[truncated]"
        return content, bool(result.is_error)

    async def _history(self, conversation_id: str) -> list[Message]:
        out: list[Message] = []
        for row in await self.db.list_messages(conversation_id):
            calls = [
                ToolCall(c["id"], c["name"], c.get("arguments") or {})
                for c in (row["tool_calls"] or [])
            ]
            out.append(
                Message(
                    row["role"],
                    row["content"] or "",
                    tool_calls=calls,
                    tool_call_id=row["tool_call_id"],
                    name=row["name"],
                )
            )
        return _drop_dangling_tool_calls(out)


def _title(message: str) -> str:
    line = " ".join(message.split())
    return (line[:57] + "…") if len(line) > 60 else (line or "New chat")


def _drop_dangling_tool_calls(messages: list[Message]) -> list[Message]:
    """Remove assistant tool calls that never got a result (e.g. an interrupted stream);
    providers reject histories where a tool_use has no matching tool_result."""
    answered = {m.tool_call_id for m in messages if m.role == "tool"}
    cleaned: list[Message] = []
    for m in messages:
        if m.role == "assistant" and m.tool_calls:
            kept = [c for c in m.tool_calls if c.id in answered]
            if not kept and not m.content:
                continue
            m = Message("assistant", m.content, tool_calls=kept)
        cleaned.append(m)
    return cleaned
