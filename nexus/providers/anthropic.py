"""Anthropic Messages API adapter (``POST /v1/messages``, SSE streaming, tool_use blocks)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from nexus.providers.base import (
    Done,
    Message,
    ModelInfo,
    Provider,
    ProviderError,
    StreamEvent,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolDef,
    iter_sse_data,
    parse_arguments,
    raise_for_status,
)

API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider(Provider):
    name = "anthropic"
    label = "Anthropic"
    default_base_url = "https://api.anthropic.com"

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._require_key(),
            "anthropic-version": API_VERSION,
            "content-type": "application/json",
        }

    async def list_models(self) -> list[ModelInfo]:
        resp = await self.http.get(
            f"{self.base_url}/v1/models", headers=self._headers(), params={"limit": 100}
        )
        await raise_for_status(resp, self.label)
        return [ModelInfo(m["id"], m.get("display_name")) for m in resp.json().get("data", [])]

    async def stream(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or DEFAULT_MAX_TOKENS,
            "messages": to_anthropic_messages(messages),
            "stream": True,
        }
        system_text = "\n\n".join(
            [system or ""] + [m.content for m in messages if m.role == "system"]
        ).strip()
        if system_text:
            body["system"] = system_text
        if temperature is not None:
            body["temperature"] = temperature
        if tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]

        blocks: dict[int, dict[str, Any]] = {}
        usage: dict[str, int] = {}
        stop_reason: str | None = None
        async with self.http.stream(
            "POST", f"{self.base_url}/v1/messages", headers=self._headers(), json=body
        ) as resp:
            await raise_for_status(resp, self.label)
            async for data in iter_sse_data(resp):
                event = json.loads(data)
                etype = event.get("type")
                if etype == "message_start":
                    u = event.get("message", {}).get("usage", {})
                    usage["input_tokens"] = u.get("input_tokens", 0)
                elif etype == "content_block_start":
                    block = event["content_block"]
                    if block.get("type") == "tool_use":
                        blocks[event["index"]] = {
                            "id": block["id"],
                            "name": block["name"],
                            "json": "",
                        }
                elif etype == "content_block_delta":
                    delta = event["delta"]
                    if delta.get("type") == "text_delta":
                        yield TextDelta(delta["text"])
                    elif delta.get("type") == "input_json_delta" and event["index"] in blocks:
                        blocks[event["index"]]["json"] += delta.get("partial_json", "")
                elif etype == "content_block_stop":
                    block = blocks.pop(event["index"], None)
                    if block is not None:
                        yield ToolCallEvent(
                            ToolCall(block["id"], block["name"], parse_arguments(block["json"]))
                        )
                elif etype == "message_delta":
                    stop_reason = event.get("delta", {}).get("stop_reason") or stop_reason
                    usage["output_tokens"] = event.get("usage", {}).get("output_tokens", 0)
                elif etype == "error":
                    err = event.get("error", {})
                    raise ProviderError(f"Anthropic stream error: {err.get('message', err)}")
        yield Done(stop_reason, usage)


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Convert neutral messages; tool results become user-role ``tool_result`` blocks and
    consecutive same-role turns are merged (the API requires strict alternation)."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            role = "user"
            content: list[dict[str, Any]] = [
                {
                    "type": "tool_result",
                    "tool_use_id": m.tool_call_id,
                    "content": m.content or "(empty)",
                    **({"is_error": True} if m.is_error else {}),
                }
            ]
        elif m.role == "assistant":
            role = "assistant"
            content = [{"type": "text", "text": m.content}] if m.content else []
            content += [
                {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                for c in m.tool_calls
            ]
            if not content:
                continue
        else:
            role = "user"
            content = [{"type": "text", "text": m.content}]
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(content)
        else:
            out.append({"role": role, "content": content})
    return out
