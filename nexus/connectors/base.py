"""The connector API — everything a plugin author needs.

A connector is a class. Decorate async (or sync) methods with :func:`tool` to
expose them as MCP tools and with :func:`resource` to expose MCP resources.
Type hints become the JSON Schema; the docstring becomes the description.

    from nexus.connectors import Connector, tool

    class Dice(Connector):
        name = "dice"
        description = "Roll dice"

        @tool()
        async def roll(self, sides: int = 6) -> int:
            \"\"\"Roll one die with the given number of sides.\"\"\"
            import random
            return random.randint(1, sides)

The tool is published as ``dice_roll``. Optional per-connector settings go in
a nested ``Config`` Pydantic model; the UI renders it as a form and the values
arrive as ``self.config``. Fields typed ``SecretStr`` are stored encrypted.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar

import httpx
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

if TYPE_CHECKING:
    from nexus.approvals import ApprovalGate
    from nexus.config import Settings
    from nexus.crypto import SecretBox
    from nexus.db import Database
    from nexus.events import EventBus

__all__ = [
    "Connector",
    "ConnectorContext",
    "ToolError",
    "ToolSpec",
    "ResourceSpec",
    "tool",
    "resource",
]

F = TypeVar("F", bound=Callable[..., Any])

TOOL_ATTR = "__nexus_tool__"
RESOURCE_ATTR = "__nexus_resource__"


@dataclass(frozen=True)
class ToolSpec:
    name: str | None
    description: str | None
    title: str | None
    requires_approval: bool
    summary: str | None
    read_only: bool | None
    destructive: bool | None
    open_world: bool | None


@dataclass(frozen=True)
class ResourceSpec:
    uri: str
    name: str | None
    description: str | None
    mime_type: str


def tool(
    name: str | None = None,
    *,
    description: str | None = None,
    title: str | None = None,
    requires_approval: bool = False,
    summary: str | None = None,
    read_only: bool | None = None,
    destructive: bool | None = None,
    open_world: bool | None = None,
) -> Callable[[F], F]:
    """Mark a connector method as an MCP tool.

    Args:
        name: tool name without the connector prefix (defaults to the method name).
        description: defaults to the method docstring.
        requires_approval: route every call through the outbound approval gate.
        summary: ``str.format`` template shown on the approval card, e.g.
            ``"Post to Discord: {content}"``. Defaults to the tool name + arguments.
        read_only / destructive / open_world: MCP tool annotation hints.
    """

    def decorator(fn: F) -> F:
        setattr(
            fn,
            TOOL_ATTR,
            ToolSpec(
                name=name,
                description=description,
                title=title,
                requires_approval=requires_approval,
                summary=summary,
                read_only=read_only,
                destructive=destructive,
                open_world=open_world,
            ),
        )
        return fn

    return decorator


def resource(
    uri: str,
    *,
    name: str | None = None,
    description: str | None = None,
    mime_type: str = "text/plain",
) -> Callable[[F], F]:
    """Mark a zero-argument connector method as an MCP resource at ``uri``."""

    def decorator(fn: F) -> F:
        setattr(fn, RESOURCE_ATTR, ResourceSpec(uri, name, description, mime_type))
        return fn

    return decorator


@dataclass
class ConnectorContext:
    """Shared services handed to every connector instance."""

    settings: Settings
    db: Database
    crypto: SecretBox
    bus: EventBus
    approvals: ApprovalGate
    http: httpx.AsyncClient


class Connector:
    """Base class for MCP Nexus connectors."""

    name: ClassVar[str] = ""
    description: ClassVar[str] = ""
    version: ClassVar[str] = "0.1.0"
    #: Optional nested Pydantic model with user-editable settings.
    Config: ClassVar[type[BaseModel] | None] = None

    def __init__(self, ctx: ConnectorContext, config: BaseModel | None = None) -> None:
        self.ctx = ctx
        self.config = config
        self.log = logging.getLogger(f"nexus.connector.{self.name}")

    async def startup(self) -> None:
        """Called after the connector is enabled, before its tools are registered."""

    async def shutdown(self) -> None:
        """Called when the connector is disabled or the server stops."""

    # Introspection used by the registry ------------------------------------
    @classmethod
    def tool_methods(cls) -> list[tuple[str, ToolSpec]]:
        return [
            (attr, getattr(member, TOOL_ATTR))
            for attr, member in _members(cls)
            if hasattr(member, TOOL_ATTR)
        ]

    @classmethod
    def resource_methods(cls) -> list[tuple[str, ResourceSpec]]:
        return [
            (attr, getattr(member, RESOURCE_ATTR))
            for attr, member in _members(cls)
            if hasattr(member, RESOURCE_ATTR)
        ]


def _members(cls: type) -> list[tuple[str, Any]]:
    seen: dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        for attr, member in vars(klass).items():
            if callable(member):
                seen[attr] = member
    return sorted(seen.items())
