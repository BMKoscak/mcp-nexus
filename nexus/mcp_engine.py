"""The MCP core: one ``MCPServer``, a connector registry, an RPC tap and the host client.

The FastAPI host never calls connector code directly. It talks to the server
through :class:`TappedTransport` — two in-memory stream pairs joined by relay
tasks that copy every JSON-RPC message to :class:`RpcTap` on the way through.
That is what the Live Inspector shows, so it is always the genuine protocol
traffic, not a reconstruction.

    host Client ⇄ [client streams] ⇄ relay+tap ⇄ [server streams] ⇄ MCPServer
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import time
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from types import TracebackType
from typing import Any

import anyio
import httpx
from mcp.client import Client
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import Implementation

from nexus import __version__
from nexus.approvals import ApprovalGate
from nexus.config import Settings
from nexus.connectors.base import ConnectorContext
from nexus.crypto import SecretBox
from nexus.db import Database
from nexus.events import EventBus
from nexus.registry import ConnectorRegistry

log = logging.getLogger("nexus.engine")

SERVER_INSTRUCTIONS = (
    "MCP Nexus exposes local connectors as tools. Tool names are prefixed with the "
    "connector name (e.g. filesystem_read_file). Tools that send data off the machine "
    "may wait for human approval and can be denied; report a denial to the user instead "
    "of retrying."
)


# ── RPC tap ──────────────────────────────────────────────────
class RpcTap:
    """Records JSON-RPC messages, pairs responses with requests, fans out to the bus."""

    def __init__(self, bus: EventBus, db: Database | None = None, *, keep: int = 2000) -> None:
        self._bus = bus
        self._db = db
        self._keep = keep
        self._seq = itertools.count(1)
        self._inflight: dict[tuple[str, str, Any], tuple[str | None, float]] = {}
        self._buffer: list[dict[str, Any]] = []
        self._flusher: asyncio.Task[None] | None = None

    def record(self, transport: str, session: str, direction: str, message: Any) -> dict[str, Any]:
        """``direction`` is ``"c2s"`` (client → server) or ``"s2c"``."""
        payload = message.model_dump(by_alias=True, exclude_none=True, mode="json")
        now = time.time()
        rpc_id = payload.get("id")
        method = payload.get("method")
        latency = None
        if method is not None:
            kind = "request" if rpc_id is not None else "notification"
            if rpc_id is not None:
                # A request travelling c2s is answered s2c, and vice versa.
                answer_dir = "s2c" if direction == "c2s" else "c2s"
                self._inflight[(session, answer_dir, rpc_id)] = (method, now)
        else:
            kind = "error" if "error" in payload else "response"
            started = self._inflight.pop((session, direction, rpc_id), None)
            if started:
                method, t0 = started
                latency = round((now - t0) * 1000, 2)
        entry = {
            "seq": next(self._seq),
            "ts": now,
            "transport": transport,
            "session": session,
            "direction": direction,
            "kind": kind,
            "method": method,
            "rpc_id": rpc_id,
            "latency_ms": latency,
            "payload": payload,
        }
        self._bus.publish("rpc", entry)
        if self._db is not None:
            self._buffer.append(entry)
        return entry

    async def start(self) -> None:
        if self._db is not None and self._flusher is None:
            self._flusher = asyncio.create_task(self._flush_loop(), name="rpc-tap-flush")

    async def stop(self) -> None:
        if self._flusher is not None:
            self._flusher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flusher
            self._flusher = None
        await self.flush()

    async def flush(self) -> None:
        if self._db is None or not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        try:
            await self._db.add_rpc_entries(batch, keep=self._keep)
        except Exception:  # pragma: no cover - logging must never break traffic
            log.exception("failed to persist rpc log batch")

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            await self.flush()


# ── transport ────────────────────────────────────────────────
class TappedTransport:
    """An MCP client ``Transport`` that runs ``server`` in-process and taps every message."""

    def __init__(
        self, server: MCPServer, tap: RpcTap, *, transport: str = "inproc", session: str = "host"
    ) -> None:
        self._server = server
        self._tap = tap
        self._transport = transport
        self._session = session
        self._cm: contextlib.AbstractAsyncContextManager[Any] | None = None

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[tuple[Any, Any]]:
        lowlevel = self._server._lowlevel_server  # noqa: SLF001 - no public accessor in mcp 2.3
        async with (
            create_client_server_memory_streams() as (
                (client_read, client_write),
                (relay_in, relay_out),
            ),
            create_client_server_memory_streams() as (
                (relay_read, relay_write),
                (server_read, server_write),
            ),
        ):

            async def relay(src: Any, dst: Any, direction: str) -> None:
                async with dst:
                    async for item in src:
                        if isinstance(item, SessionMessage):
                            try:
                                self._tap.record(
                                    self._transport, self._session, direction, item.message
                                )
                            except Exception:  # pragma: no cover
                                log.exception("rpc tap failed")
                        await dst.send(item)

            async with anyio.create_task_group() as tg:
                tg.start_soon(relay, relay_in, relay_write, "c2s")
                tg.start_soon(relay, relay_read, relay_out, "s2c")
                tg.start_soon(
                    lambda: lowlevel.run(
                        server_read, server_write, lowlevel.create_initialization_options()
                    )
                )
                try:
                    yield client_read, client_write
                finally:
                    await client_write.aclose()
                    tg.cancel_scope.cancel()

    async def __aenter__(self) -> tuple[Any, Any]:
        self._cm = self._connect()
        return await self._cm.__aenter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None:
        assert self._cm is not None
        return await self._cm.__aexit__(exc_type, exc, tb)


# ── engine ───────────────────────────────────────────────────
class MCPEngine:
    """Owns the server, registry, tap and the host's own MCP client session."""

    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        bus: EventBus | None = None,
        crypto: SecretBox | None = None,
        approvals: ApprovalGate | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings.ensure_dirs()
        self._http_transport = http_transport  # tests inject httpx.MockTransport here
        self.settings = settings
        self.db = db or Database(settings.db_path)
        self.bus = bus or EventBus()
        self.crypto = crypto or SecretBox.from_settings(settings)
        self.approvals = approvals or ApprovalGate(
            self.db,
            self.bus,
            default_auto_approve=settings.auto_approve_outbound,
            timeout_seconds=settings.approval_timeout_seconds,
        )
        self.server: MCPServer = MCPServer(
            "MCP Nexus", instructions=SERVER_INSTRUCTIONS, version=__version__
        )
        self.tap = RpcTap(self.bus, self.db)
        self.registry = ConnectorRegistry(self)
        self.http: httpx.AsyncClient | None = None
        self.ctx: ConnectorContext | None = None
        self._client: Client | None = None
        self._runner: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._stack: AsyncExitStack | None = None
        self._owns_db = db is None

    # lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        if self._owns_db or self.db._conn is None:  # noqa: SLF001
            await self.db.connect()
        self._stack = AsyncExitStack()
        self.http = await self._stack.enter_async_context(
            httpx.AsyncClient(
                timeout=self.settings.fetch_timeout_seconds,
                headers={"User-Agent": f"MCP-Nexus/{__version__}"},
                follow_redirects=False,
                transport=self._http_transport,
            )
        )
        self.ctx = ConnectorContext(
            settings=self.settings,
            db=self.db,
            crypto=self.crypto,
            bus=self.bus,
            approvals=self.approvals,
            http=self.http,
        )
        await self.tap.start()
        await self.registry.load_all()
        # The client session owns anyio task groups, which must be entered and exited in the
        # same task. Run it in a dedicated supervisor task so start()/stop() may be called
        # from different tasks (FastAPI lifespan, pytest fixtures).
        self._stop_event = asyncio.Event()
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._runner = asyncio.create_task(self._run_client(ready), name="mcp-host-client")
        await ready
        log.info("MCP engine started with %d tools", len(await self.server.list_tools()))

    async def _run_client(self, ready: asyncio.Future[None]) -> None:
        try:
            async with Client(
                TappedTransport(self.server, self.tap),
                cache=None,  # tools change at runtime; never serve a stale tools/list
                client_info=Implementation(name="mcp-nexus-host", version=__version__),
            ) as client:
                self._client = client
                ready.set_result(None)
                await self._stop_event.wait()
        except BaseException as exc:
            if not ready.done():
                ready.set_exception(exc)
                return
            raise
        finally:
            self._client = None

    async def stop(self) -> None:
        self.approvals.cancel_all()
        if self._runner is not None:
            self._stop_event.set()
            try:
                await asyncio.wait_for(self._runner, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                self._runner.cancel()
            except Exception:  # pragma: no cover
                log.exception("host client shut down with an error")
            self._runner = None
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = None
        await self.registry.shutdown()
        await self.tap.stop()
        if self._owns_db:
            await self.db.close()

    async def __aenter__(self) -> MCPEngine:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stop()

    # host-side MCP calls (all go through the tapped transport) ---------
    @property
    def client(self) -> Client:
        if self._client is None:
            raise RuntimeError("MCPEngine is not started")
        return self._client

    async def list_tools(self) -> list[Any]:
        return list((await self.client.list_tools()).tools)

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        return await self.client.call_tool(name, arguments or {})

    async def list_prompts(self) -> list[Any]:
        return list((await self.client.list_prompts()).prompts)

    async def get_prompt(self, name: str, arguments: dict[str, str] | None = None) -> Any:
        return await self.client.get_prompt(name, arguments or {})

    async def list_resources(self) -> list[Any]:
        return list((await self.client.list_resources()).resources)

    async def read_resource(self, uri: str) -> Any:
        return await self.client.read_resource(uri)
