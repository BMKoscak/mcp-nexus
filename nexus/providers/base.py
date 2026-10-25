"""Provider-neutral chat types and the adapter protocol.

Every adapter converts this shape to its vendor's wire format and back, so the
agent loop never sees vendor-specific JSON.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx

Role = Literal["system", "user", "assistant", "tool"]


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict[str, Any]


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None  # tool name, for role="tool"
    is_error: bool = False


# ── stream events ────────────────────────────────────────────
@dataclass
class TextDelta:
    text: str


@dataclass
class ToolCallEvent:
    call: ToolCall


@dataclass
class Done:
    stop_reason: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


StreamEvent = TextDelta | ToolCallEvent | Done


@dataclass
class ModelInfo:
    id: str
    name: str | None = None


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Provider(ABC):
    """One LLM backend. Instances are cheap; the router builds them per request."""

    name: str = ""
    label: str = ""
    default_base_url: str = ""
    needs_api_key: bool = True

    def __init__(
        self, http: httpx.AsyncClient, *, api_key: str | None, base_url: str | None
    ) -> None:
        self.http = http
        self.api_key = api_key
        self.base_url = (base_url or self.default_base_url).rstrip("/")

    @abstractmethod
    async def list_models(self) -> list[ModelInfo]: ...

    @abstractmethod
    def stream(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolDef] | None = None,
        *,
        system: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamEvent]: ...

    def _require_key(self) -> str:
        if self.needs_api_key and not self.api_key:
            raise ProviderError(f"No API key configured for {self.label or self.name}.")
        return self.api_key or ""


# ── shared helpers ───────────────────────────────────────────
async def raise_for_status(resp: httpx.Response, provider: str) -> None:
    if resp.status_code < 400:
        return
    body = (await resp.aread()).decode("utf-8", errors="replace")
    message = body
    try:
        data = json.loads(body)
        err = data.get("error", data)
        message = err.get("message") if isinstance(err, dict) else str(err)
    except (json.JSONDecodeError, AttributeError):
        pass
    raise ProviderError(
        f"{provider} returned HTTP {resp.status_code}: {message}"[:800], resp.status_code
    )


async def iter_sse_data(resp: httpx.Response) -> AsyncIterator[str]:
    """Yield the ``data:`` payloads of a server-sent-events response."""
    data_lines: list[str] = []
    async for line in resp.aiter_lines():
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield "\n".join(data_lines)


def parse_arguments(raw: str | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"value": value}
