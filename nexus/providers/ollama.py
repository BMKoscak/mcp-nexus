"""Ollama adapter (``POST /api/chat``, NDJSON streaming, native tool calling)."""

from __future__ import annotations

import json
import uuid
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
    parse_arguments,
    raise_for_status,
)


class OllamaProvider(Provider):
    name = "ollama"
    label = "Ollama"
    default_base_url = "http://localhost:11434"
    needs_api_key = False

    async def list_models(self) -> list[ModelInfo]:
        resp = await self.http.get(f"{self.base_url}/api/tags")
        await raise_for_status(resp, self.label)
        return [ModelInfo(m["name"]) for m in resp.json().get("models", [])]

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
            "messages": to_ollama_messages(messages, system),
            "stream": True,
        }
        options: dict[str, Any] = {}
        if temperature is not None:
            options["temperature"] = temperature
        if max_tokens:
            options["num_predict"] = max_tokens
        if options:
            body["options"] = options
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        usage: dict[str, int] = {}
        stop_reason: str | None = None
        async with self.http.stream("POST", f"{self.base_url}/api/chat", json=body) as resp:
            await raise_for_status(resp, self.label)
            async for line in resp.aiter_lines():
                if not line.strip():
                    continue
                chunk = json.loads(line)
                if "error" in chunk:
                    raise ProviderError(f"Ollama error: {chunk['error']}")
                msg = chunk.get("message") or {}
                if msg.get("content"):
                    yield TextDelta(msg["content"])
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    yield ToolCallEvent(
                        ToolCall(
                            tc.get("id") or f"call_{uuid.uuid4().hex[:8]}",
                            fn.get("name", ""),
                            parse_arguments(fn.get("arguments")),
                        )
                    )
                if chunk.get("done"):
                    stop_reason = chunk.get("done_reason")
                    usage = {
                        "input_tokens": chunk.get("prompt_eval_count", 0),
                        "output_tokens": chunk.get("eval_count", 0),
                    }
        yield Done(stop_reason, usage)


def to_ollama_messages(messages: list[Message], system: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": c.name, "arguments": c.arguments}} for c in m.tool_calls
                ]
            out.append(entry)
        elif m.role == "tool":
            out.append({"role": "tool", "content": m.content, "tool_name": m.name or ""})
        else:
            out.append({"role": m.role, "content": m.content})
    return out
