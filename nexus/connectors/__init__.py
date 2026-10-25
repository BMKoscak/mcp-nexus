"""Connector API and built-in connectors.

Plugin authors only need::

    from nexus.connectors import Connector, tool, resource, ToolError
"""

from nexus.connectors.base import (
    Connector,
    ConnectorContext,
    ResourceSpec,
    ToolError,
    ToolSpec,
    resource,
    tool,
)

#: Dotted paths of the connectors that ship with MCP Nexus.
BUILTIN_CONNECTORS = [
    "nexus.connectors.filesystem:FilesystemConnector",
    "nexus.connectors.outbound:OutboundConnector",
    "nexus.connectors.web_fetch:WebFetchConnector",
    "nexus.connectors.system:SystemConnector",
    "nexus.connectors.sqlite_query:SQLiteQueryConnector",
]

__all__ = [
    "BUILTIN_CONNECTORS",
    "Connector",
    "ConnectorContext",
    "ResourceSpec",
    "ToolError",
    "ToolSpec",
    "resource",
    "tool",
]
