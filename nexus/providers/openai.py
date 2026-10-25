"""OpenAI Chat Completions adapter. Also works with OpenAI-compatible servers
(LM Studio, vLLM, OpenRouter, llama.cpp) by changing the base URL."""

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


class OpenAIProvider(Provider):
    name = "openai"
    label = "OpenAI"
    default_base_url = "https://api.openai.com/v1"

    def _headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        key = self._require_key() if self._is_official() else (self.api_key or "")
        if key:
            headers["authorization"] = f"Bearer {key}"
        return headers

    def _is_official(self) -> bool:
        return "api.openai.com" in self.base_url

    async def list_models(self) -> list[ModelInfo]:
        resp = await self.http.get(f"{self.base_url}/models", headers=self._headers())
        await raise_for_status(resp, self.label)
        return sorted((ModelInfo(m["id"]) for m in resp.json().get("data", [])), key=lambda m: m.id)

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
            "messages": to_openai_messages(messages, system),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if temperature is not None:
            body["temperature"] = temperature
        if max_tokens:
            body["max_completion_tokens"] = max_tokens
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

        calls: dict[int, dict[str, str]] = {}
        stop_reason: str | None = None
        usage: dict[str, int] = {}
        async with self.http.stream(
            "POST", f"{self.base_url}/chat/completions", headers=self._headers(), json=body
        ) as resp:
            await raise_for_status(resp, self.label)
            async for data in iter_sse_data(resp):
                if data.strip() == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    err = chunk["error"]
                    raise ProviderError(f"OpenAI stream error: {err.get('message', err)}")
                if chunk.get("usage"):
                    u = chunk["usage"]
                    usage = {
                        "input_tokens": u.get("prompt_tokens", 0),
                        "output_tokens": u.get("completion_tokens", 0),
                    }
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    if delta.get("content"):
                        yield TextDelta(delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        slot = calls.setdefault(
                            tc.get("index", 0), {"id": "", "name": "", "args": ""}
                        )
                        slot["id"] = tc.get("id") or slot["id"]
                        fn = tc.get("function") or {}
                        slot["name"] = fn.get("name") or slot["name"]
                        slot["args"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        stop_reason = choice["finish_reason"]
        for index in sorted(calls):
            slot = calls[index]
            yield ToolCallEvent(
                ToolCall(slot["id"] or f"call_{index}", slot["name"], parse_arguments(slot["args"]))
            )
        yield Done(stop_reason, usage)


def to_openai_messages(messages: list[Message], system: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "assistant":
            entry: dict[str, Any] = {"role": "assistant", "content": m.content or None}
            if m.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                    }
                    for c in m.tool_calls
                ]
            out.append(entry)
        elif m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content})
        else:
            out.append({"role": m.role, "content": m.content})
    return out
