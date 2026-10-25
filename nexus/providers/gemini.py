"""Google Gemini adapter (Generative Language API, ``streamGenerateContent`` over SSE).

Wire format notes, all handled here so the agent loop stays provider-neutral:

* Tool schemas go in ``parametersJsonSchema``, which accepts JSON Schema directly
  (the older ``parameters`` field only takes an OpenAPI subset and rejects keywords
  MCP tools commonly use). The key is omitted for tools without parameters.
* Gemini 3+ attaches a ``thoughtSignature`` to function-call parts and rejects the next
  request with HTTP 400 if it isn't echoed back on the same part. It is stored in
  ``ToolCall.extra`` and replayed verbatim.
* Parallel calls must be answered as FC1, FC2, FR1, FR2: all function calls in one
  ``model`` turn, then all function responses together in one ``user`` turn.
* ``functionCall.id`` is only sent back when Gemini supplied one.
"""

from __future__ import annotations

import copy
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
    iter_sse_data,
    parse_arguments,
    raise_for_status,
)

API_VERSION = "v1beta"


class GeminiProvider(Provider):
    name = "gemini"
    label = "Google Gemini"
    default_base_url = "https://generativelanguage.googleapis.com"

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self._require_key(), "content-type": "application/json"}

    async def list_models(self) -> list[ModelInfo]:
        models: list[ModelInfo] = []
        token: str | None = None
        for _ in range(10):  # pagination guard
            params: dict[str, Any] = {"pageSize": 1000}
            if token:
                params["pageToken"] = token
            resp = await self.http.get(
                f"{self.base_url}/{API_VERSION}/models", headers=self._headers(), params=params
            )
            await raise_for_status(resp, self.label)
            data = resp.json()
            for m in data.get("models", []):
                if "generateContent" not in (m.get("supportedGenerationMethods") or []):
                    continue  # embeddings, TTS-only, etc.
                model_id = m["name"].removeprefix("models/")
                models.append(ModelInfo(model_id, m.get("displayName") or model_id))
            token = data.get("nextPageToken")
            if not token:
                break
        return models

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
        body: dict[str, Any] = {"contents": to_gemini_contents(messages)}
        system_text = "\n\n".join(
            [system or ""] + [m.content for m in messages if m.role == "system"]
        ).strip()
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        config: dict[str, Any] = {}
        if temperature is not None:
            config["temperature"] = temperature
        if max_tokens:
            config["maxOutputTokens"] = max_tokens
        if config:
            body["generationConfig"] = config
        if tools:
            body["tools"] = [{"functionDeclarations": [to_function_declaration(t) for t in tools]}]

        model_path = model if model.startswith("models/") else f"models/{model}"
        url = f"{self.base_url}/{API_VERSION}/{model_path}:streamGenerateContent"
        usage: dict[str, int] = {}
        stop_reason: str | None = None
        async with self.http.stream(
            "POST", url, params={"alt": "sse"}, headers=self._headers(), json=body
        ) as resp:
            await raise_for_status(resp, self.label)
            async for data in iter_sse_data(resp):
                chunk = json.loads(data)
                if "error" in chunk:
                    err = chunk["error"]
                    raise ProviderError(f"Gemini stream error: {err.get('message', err)}")
                if chunk.get("usageMetadata"):
                    u = chunk["usageMetadata"]
                    usage = {
                        "input_tokens": u.get("promptTokenCount", 0),
                        "output_tokens": u.get("candidatesTokenCount", 0),
                    }
                feedback = chunk.get("promptFeedback") or {}
                if feedback.get("blockReason") and not chunk.get("candidates"):
                    raise ProviderError(f"Gemini blocked the prompt: {feedback['blockReason']}")
                for cand in (chunk.get("candidates") or [])[:1]:
                    for part in (cand.get("content") or {}).get("parts") or []:
                        if part.get("thought"):
                            continue  # thought summaries aren't part of the answer
                        if part.get("functionCall"):
                            yield ToolCallEvent(_tool_call(part))
                        elif part.get("text"):
                            yield TextDelta(part["text"])
                    stop_reason = cand.get("finishReason") or stop_reason
        yield Done(stop_reason, usage)


def _tool_call(part: dict[str, Any]) -> ToolCall:
    fc = part["functionCall"]
    extra: dict[str, Any] = {}
    if part.get("thoughtSignature"):
        extra["thoughtSignature"] = part["thoughtSignature"]
    if fc.get("id"):
        extra["gemini_id"] = fc["id"]
    return ToolCall(
        fc.get("id") or f"call_{uuid.uuid4().hex[:10]}",
        fc.get("name", ""),
        parse_arguments(fc.get("args")),
        extra,
    )


def to_function_declaration(tool: ToolDef) -> dict[str, Any]:
    decl: dict[str, Any] = {"name": tool.name, "description": tool.description}
    schema = clean_schema(tool.parameters)
    if schema.get("properties"):
        decl["parametersJsonSchema"] = schema
    return decl


def clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local ``$ref``s (dropping ``$defs``) and strip ``$schema``.

    ``parametersJsonSchema`` takes JSON Schema as-is, so this is deliberately minimal:
    only references are resolved, because a schema whose ``$defs`` are removed must not
    keep pointers into them.
    """
    schema = copy.deepcopy(schema or {})
    defs = {**schema.pop("$defs", {}), **schema.pop("definitions", {})}
    schema.pop("$schema", None)

    def resolve(node: Any, depth: int = 0) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if (
                isinstance(ref, str)
                and ref.startswith(("#/$defs/", "#/definitions/"))
                and depth < 20
            ):
                target = defs.get(ref.rsplit("/", 1)[-1], {})
                merged = {**target, **{k: v for k, v in node.items() if k != "$ref"}}
                return resolve(merged, depth + 1)
            return {k: resolve(v, depth) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v, depth) for v in node]
        return node

    return resolve(schema)


def to_gemini_contents(messages: list[Message]) -> list[dict[str, Any]]:
    """Neutral messages → Gemini ``contents``.

    Assistant turns become ``role: model``; tool results become ``functionResponse``
    parts in a ``user`` turn. Consecutive turns with the same role are merged, which
    keeps every function call of a step together, followed by all of its responses.
    """
    calls_by_id: dict[str, ToolCall] = {}
    out: list[dict[str, Any]] = []

    def push(role: str, parts: list[dict[str, Any]]) -> None:
        if not parts:
            return
        if out and out[-1]["role"] == role:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": parts})

    for m in messages:
        if m.role == "system":
            continue
        if m.role == "user":
            push("user", [{"text": m.content or " "}])
        elif m.role == "assistant":
            parts: list[dict[str, Any]] = []
            if m.content:
                parts.append({"text": m.content})
            for call in m.tool_calls:
                calls_by_id[call.id] = call
                fc: dict[str, Any] = {"name": call.name, "args": call.arguments}
                if call.extra.get("gemini_id"):
                    fc["id"] = call.extra["gemini_id"]
                part: dict[str, Any] = {"functionCall": fc}
                if call.extra.get("thoughtSignature"):
                    part["thoughtSignature"] = call.extra["thoughtSignature"]
                parts.append(part)
            push("model", parts)
        elif m.role == "tool":
            call = calls_by_id.get(m.tool_call_id or "")
            fr: dict[str, Any] = {
                "name": m.name or (call.name if call else ""),
                "response": {"error": m.content} if m.is_error else {"result": m.content},
            }
            if call and call.extra.get("gemini_id"):
                fr["id"] = call.extra["gemini_id"]
            push("user", [{"functionResponse": fr}])
    return out
