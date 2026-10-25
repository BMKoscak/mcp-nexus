"""Connector discovery, lifecycle and MCP registration.

Sources, in load order:
    1. built-ins listed in ``nexus.connectors.BUILTIN_CONNECTORS``
    2. ``*.py`` files in ``NEXUS_PLUGINS_DIR`` (files starting with ``_`` are skipped)
    3. installed packages exposing the ``mcp_nexus.connectors`` entry-point group

A connector that fails to import, validate or start is recorded with status
``error`` and its traceback; it never prevents the rest of the hub starting.
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import inspect
import re
import sys
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resources import FunctionResource
from mcp.types import ToolAnnotations
from pydantic import BaseModel, SecretStr, ValidationError

from nexus.connectors import BUILTIN_CONNECTORS
from nexus.connectors.base import Connector, ToolSpec

if TYPE_CHECKING:
    from nexus.mcp_engine import MCPEngine

Source = Literal["builtin", "plugin", "entrypoint"]
Status = Literal["active", "disabled", "error"]

CONNECTOR_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
TOOL_NAME_RE = re.compile(
    r"^[A-Za-z0-9_-]{1,64}$"
)  # strictest common denominator (Anthropic/OpenAI)
ENTRY_POINT_GROUP = "mcp_nexus.connectors"
ENC_MARKER = "__enc__"
SUMMARY_VALUE_LIMIT = 300


@dataclass
class ConnectorRecord:
    name: str
    source: Source
    origin: str
    cls: type[Connector] | None = None
    status: Status = "disabled"
    error: str | None = None
    instance: Connector | None = None
    tools: list[str] = field(default_factory=list)
    resources: list[str] = field(default_factory=list)

    def public(self, crypto_mask: Callable[[str], str] | None = None) -> dict[str, Any]:
        cls = self.cls
        tools = []
        if cls is not None:
            for attr, spec in cls.tool_methods():
                tools.append(
                    {
                        "name": f"{self.name}_{spec.name or attr}",
                        "description": spec.description
                        or (inspect.getdoc(getattr(cls, attr)) or "").split("\n\n")[0],
                        "requires_approval": spec.requires_approval,
                    }
                )
        return {
            "name": self.name,
            "description": getattr(cls, "description", "") if cls else "",
            "version": getattr(cls, "version", "") if cls else "",
            "source": self.source,
            "origin": self.origin,
            "status": self.status,
            "error": self.error,
            "tools": tools,
            "resources": self.resources,
            "has_config": bool(cls and cls.Config),
        }


class ConnectorRegistry:
    def __init__(self, engine: MCPEngine) -> None:
        self.engine = engine
        self.records: dict[str, ConnectorRecord] = {}

    # ── discovery ────────────────────────────────────────────
    def discover(self) -> list[ConnectorRecord]:
        found: list[ConnectorRecord] = []
        for dotted in BUILTIN_CONNECTORS:
            found.extend(self._from_dotted(dotted, "builtin"))
        found.extend(self._from_plugins_dir())
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            found.extend(self._from_dotted(ep.value, "entrypoint", label=ep.name))
        return found

    def _from_dotted(
        self, dotted: str, source: Source, label: str | None = None
    ) -> list[ConnectorRecord]:
        module_name, _, attr = dotted.partition(":")
        try:
            module = importlib.import_module(module_name)
            classes = [getattr(module, attr)] if attr else _connector_classes(module)
        except Exception:
            return [_error_record(label or dotted, source, dotted)]
        return [ConnectorRecord(name=c.name, source=source, origin=dotted, cls=c) for c in classes]

    def _from_plugins_dir(self) -> list[ConnectorRecord]:
        plugins_dir = self.engine.settings.plugins_dir
        if not plugins_dir.is_dir():
            return []
        records: list[ConnectorRecord] = []
        for path in sorted(plugins_dir.glob("*.py")):
            if path.name.startswith("_"):
                continue
            try:
                module = _import_file(path)
                classes = _connector_classes(module)
            except Exception:
                records.append(_error_record(path.stem, "plugin", str(path)))
                continue
            records.extend(
                ConnectorRecord(name=c.name, source="plugin", origin=str(path), cls=c)
                for c in classes
            )
        return records

    # ── lifecycle ────────────────────────────────────────────
    async def load_all(self) -> None:
        for record in self.discover():
            await self._add(record)

    async def _add(self, record: ConnectorRecord) -> None:
        if record.status == "error":
            self.records.setdefault(record.name, record)
            self._emit(record, "error")
            return
        problem = _validate_class(record.cls)
        if problem is None and record.name in self.records:
            first = self.records[record.name].origin
            problem = f"duplicate connector name {record.name!r} (already loaded from {first})"
            record.name = f"{record.name}@{record.source}"
        if problem:
            record.status, record.error = "error", problem
            self.records[record.name] = record
            self._emit(record, "error")
            return
        self.records[record.name] = record
        state = await self.engine.db.get_connector_state(record.name)
        if state is None or state["enabled"]:
            await self.activate(record.name)
        else:
            self._emit(record, "disabled")

    async def activate(self, name: str) -> ConnectorRecord:
        record = self._get(name)
        if record.status == "active" or record.cls is None:
            return record
        try:
            config = await self._load_config(record)
            instance = record.cls(self.engine.ctx, config)
            await instance.startup()
            record.instance = instance
            self._register(record)
            record.status, record.error = "active", None
        except Exception as exc:
            self._unregister(record)
            record.instance = None
            record.status = "error"
            record.error = _format_exc(exc)
        self._emit(record, record.status)
        return record

    async def deactivate(self, name: str, *, status: Status = "disabled") -> ConnectorRecord:
        record = self._get(name)
        self._unregister(record)
        if record.instance is not None:
            try:
                await record.instance.shutdown()
            except Exception:  # pragma: no cover - best effort
                pass
            record.instance = None
        if record.status != "error" or status == "disabled":
            record.status = status
        self._emit(record, record.status)
        return record

    async def set_enabled(self, name: str, enabled: bool) -> ConnectorRecord:
        record = self._get(name)
        await self.engine.db.set_connector_state(record.name, enabled=enabled)
        if enabled:
            return await self.activate(name)
        return await self.deactivate(name)

    async def shutdown(self) -> None:
        for name in list(self.records):
            if self.records[name].status == "active":
                await self.deactivate(name)

    async def reload_plugins(self) -> None:
        """Re-import plugin files and entry points; built-ins stay as they are."""
        for name, record in list(self.records.items()):
            if record.source != "builtin":
                await self.deactivate(name)
                del self.records[name]
        found = self._from_plugins_dir()
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            found.extend(self._from_dotted(ep.value, "entrypoint", label=ep.name))
        for record in found:
            await self._add(record)

    # ── config ───────────────────────────────────────────────
    def config_view(self, name: str) -> dict[str, Any]:
        """JSON Schema + current values (secrets masked) for the UI form."""
        record = self._get(name)
        model = record.cls.Config if record.cls else None
        if model is None:
            return {"schema": None, "values": {}}
        values: dict[str, Any] = {}
        current = record.instance.config if record.instance else None
        for field_name, info in model.model_fields.items():
            value = getattr(current, field_name, info.default) if current else info.default
            if isinstance(value, SecretStr):
                value = "••••••••" if value.get_secret_value() else ""
            elif isinstance(value, Path):
                value = str(value)
            values[field_name] = value
        return {"schema": model.model_json_schema(), "values": values}

    async def update_config(self, name: str, values: dict[str, Any]) -> ConnectorRecord:
        record = self._get(name)
        model = record.cls.Config if record.cls else None
        if model is None:
            raise ValueError(f"connector {name!r} has no configuration")
        state = await self.engine.db.get_connector_state(record.name) or {"config": {}}
        stored = dict(state["config"])
        for key, value in values.items():
            info = model.model_fields.get(key)
            if info is None:
                continue
            if _is_secret(info.annotation):
                if value in (None, "", "••••••••"):
                    continue  # unchanged
                stored[key] = {ENC_MARKER: self.engine.crypto.encrypt(str(value))}
            else:
                stored[key] = value
        model.model_validate(self._decrypt_config(stored))  # raises ValidationError on bad input
        await self.engine.db.set_connector_state(record.name, config=stored)
        if record.status == "active":
            await self.deactivate(name)
            return await self.activate(name)
        return record

    async def _load_config(self, record: ConnectorRecord) -> BaseModel | None:
        model = record.cls.Config if record.cls else None
        if model is None:
            return None
        state = await self.engine.db.get_connector_state(record.name)
        raw = self._decrypt_config(state["config"] if state else {})
        try:
            return model.model_validate(raw)
        except ValidationError:
            # Stored config no longer matches the model (e.g. after an upgrade): use defaults.
            return model()

    def _decrypt_config(self, stored: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in stored.items():
            if isinstance(value, dict) and ENC_MARKER in value:
                out[key] = self.engine.crypto.decrypt(value[ENC_MARKER]) or ""
            else:
                out[key] = value
        return out

    # ── MCP registration ─────────────────────────────────────
    def _register(self, record: ConnectorRecord) -> None:
        server = self.engine.server
        instance = record.instance
        assert instance is not None and record.cls is not None
        for attr, spec in record.cls.tool_methods():
            full_name = f"{record.name}_{spec.name or attr}"
            if not TOOL_NAME_RE.match(full_name):
                raise ValueError(
                    f"invalid tool name {full_name!r} (allowed: A-Z a-z 0-9 _ -, max 64)"
                )
            fn = getattr(instance, attr)
            description = spec.description or inspect.getdoc(fn) or full_name
            if spec.requires_approval:
                fn = self._gated(fn, full_name, record.name, spec)
            annotations = None
            if any(
                v is not None
                for v in (spec.title, spec.read_only, spec.destructive, spec.open_world)
            ):
                annotations = ToolAnnotations(
                    title=spec.title,
                    read_only_hint=spec.read_only,
                    destructive_hint=spec.destructive,
                    open_world_hint=spec.open_world,
                )
            server.add_tool(
                fn,
                name=full_name,
                title=spec.title,
                description=description,
                annotations=annotations,
                meta={
                    "nexus/connector": record.name,
                    "nexus/requiresApproval": spec.requires_approval,
                },
            )
            record.tools.append(full_name)
        for attr, rspec in record.cls.resource_methods():
            fn = getattr(instance, attr)
            server.add_resource(
                FunctionResource(
                    uri=rspec.uri,
                    name=rspec.name or f"{record.name}_{attr}",
                    description=rspec.description or inspect.getdoc(fn),
                    mime_type=rspec.mime_type,
                    fn=fn,
                )
            )
            record.resources.append(rspec.uri)

    def _unregister(self, record: ConnectorRecord) -> None:
        server = self.engine.server
        for tool_name in record.tools:
            try:
                server.remove_tool(tool_name)
            except Exception:
                pass
        # MCPServer has no public remove_resource yet; drop it from the manager directly.
        resources = getattr(server._resource_manager, "_resources", {})  # noqa: SLF001
        for uri in record.resources:
            resources.pop(uri, None)
        record.tools.clear()
        record.resources.clear()

    def _gated(
        self, fn: Callable[..., Any], tool_name: str, connector: str, spec: ToolSpec
    ) -> Callable[..., Any]:
        gate = self.engine.approvals

        @functools.wraps(fn)
        async def gated(**kwargs: Any) -> Any:
            decision = await gate.request(
                tool=tool_name,
                connector=connector,
                summary=_summarize(spec.summary, tool_name, kwargs),
                arguments=_jsonable(kwargs),
            )
            if decision not in ("approved", "auto"):
                reason = (
                    "timed out waiting for approval"
                    if decision == "timeout"
                    else "denied by the user"
                )
                raise ToolError(f"Action not performed: {reason}.")
            result = fn(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result

        return gated

    # ── helpers ──────────────────────────────────────────────
    def _get(self, name: str) -> ConnectorRecord:
        try:
            return self.records[name]
        except KeyError:
            raise KeyError(f"unknown connector {name!r}") from None

    def _emit(self, record: ConnectorRecord, event: str) -> None:
        self.engine.bus.publish("connectors", {"type": event, "connector": record.public()})

    def public(self) -> list[dict[str, Any]]:
        return [r.public() for r in self.records.values()]


# ── module-level helpers ─────────────────────────────────────
def _connector_classes(module: Any) -> list[type[Connector]]:
    return [
        obj
        for obj in vars(module).values()
        if inspect.isclass(obj)
        and issubclass(obj, Connector)
        and obj is not Connector
        and obj.__module__ == module.__name__
        and not inspect.isabstract(obj)
    ]


def _import_file(path: Path) -> Any:
    module_name = f"nexus_plugin_{path.stem}"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _validate_class(cls: type[Connector] | None) -> str | None:
    if cls is None:
        return "no connector class"
    if not CONNECTOR_NAME_RE.match(cls.name or ""):
        return f"connector name {cls.name!r} must match {CONNECTOR_NAME_RE.pattern}"
    if not cls.tool_methods() and not cls.resource_methods():
        return "connector defines no @tool or @resource methods"
    if cls.Config is not None and not (
        inspect.isclass(cls.Config) and issubclass(cls.Config, BaseModel)
    ):
        return "Config must be a pydantic BaseModel subclass"
    return None


def _error_record(name: str, source: Source, origin: str) -> ConnectorRecord:
    return ConnectorRecord(
        name=re.sub(r"[^a-z0-9_]", "_", name.lower().rsplit(".", 1)[-1].split(":")[-1])
        or "unknown",
        source=source,
        origin=origin,
        status="error",
        error=traceback.format_exc(limit=5),
    )


def _format_exc(exc: BaseException) -> str:
    return "".join(traceback.format_exception(exc, limit=5))


def _is_secret(annotation: Any) -> bool:
    if annotation is SecretStr:
        return True
    return SecretStr in getattr(annotation, "__args__", ())


def _jsonable(kwargs: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in kwargs.items():
        if isinstance(value, BaseModel):
            out[key] = value.model_dump(mode="json")
        elif isinstance(value, SecretStr):
            out[key] = "••••••••"
        else:
            out[key] = value
    return out


def _summarize(template: str | None, tool_name: str, kwargs: dict[str, Any]) -> str:
    short = {k: _shorten(v) for k, v in kwargs.items()}
    if template:
        try:
            return template.format(**short)
        except (KeyError, IndexError, ValueError):
            pass
    args = ", ".join(f"{k}={v!r}" for k, v in short.items())
    return f"{tool_name}({args})"


def _shorten(value: Any) -> Any:
    if value is None:
        return ""
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= SUMMARY_VALUE_LIMIT else text[:SUMMARY_VALUE_LIMIT] + "…"
