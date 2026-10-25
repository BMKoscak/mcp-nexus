"""Human-in-the-loop gate for tools that send data off the machine.

A tool marked ``requires_approval=True`` calls :meth:`ApprovalGate.request`
before it runs. With auto-approve off, the request is written to the
``pending_actions`` table and published on the ``approvals`` topic; the call
then waits until someone decides or the timeout expires (→ denied).

The decision travels through SQLite, not just process memory, so approvals
also work across processes: when Claude Desktop runs ``nexus stdio``, that
process waits on the row while the web UI (a different process) decides it.
Same-process decisions additionally resolve an in-memory future instantly.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any, Literal

from nexus.db import Database
from nexus.events import EventBus

Decision = Literal["approved", "denied", "timeout", "auto"]
SETTING_KEY = "auto_approve_outbound"
POLL_SECONDS = 0.5


class ApprovalGate:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        *,
        default_auto_approve: bool = False,
        timeout_seconds: float = 120,
    ) -> None:
        self._db = db
        self._bus = bus
        self._default_auto = default_auto_approve
        self.timeout_seconds = timeout_seconds
        self._futures: dict[str, asyncio.Future[Decision]] = {}

    async def auto_approve(self) -> bool:
        return bool(await self._db.get_setting(SETTING_KEY, self._default_auto))

    async def set_auto_approve(self, value: bool) -> None:
        await self._db.set_setting(SETTING_KEY, bool(value))
        self._bus.publish("approvals", {"type": "auto_approve", "value": bool(value)})

    async def pending(self) -> list[dict[str, Any]]:
        """Pending actions from *any* process that are still inside their timeout window."""
        cutoff = time.time() - self.timeout_seconds - 5
        return [
            _public(r)
            for r in await self._db.list_pending_actions("pending")
            if r["created_at"] >= cutoff
        ]

    async def history(self, limit: int = 50) -> list[dict[str, Any]]:
        return [
            _public(r) | {"status": r["status"], "decided_at": r["decided_at"]}
            for r in await self._db.list_pending_actions(limit=limit)
        ]

    async def request(
        self, *, tool: str, connector: str, summary: str, arguments: dict[str, Any]
    ) -> Decision:
        aid = uuid.uuid4().hex[:12]
        await self._db.add_pending_action(
            aid, tool=tool, connector=connector, summary=summary, arguments=arguments
        )
        event = {
            "id": aid,
            "tool": tool,
            "connector": connector,
            "summary": summary,
            "arguments": arguments,
            "created_at": time.time(),
        }

        if await self.auto_approve():
            await self._db.resolve_pending_action(aid, "auto")
            self._bus.publish("approvals", {"type": "auto", **event})
            return "auto"

        future: asyncio.Future[Decision] = asyncio.get_running_loop().create_future()
        self._futures[aid] = future
        self._bus.publish("approvals", {"type": "requested", **event})
        deadline = time.monotonic() + self.timeout_seconds
        decision: Decision | None = None
        try:
            while decision is None:
                try:
                    decision = await asyncio.wait_for(asyncio.shield(future), POLL_SECONDS)
                except TimeoutError:
                    row = await self._db.get_pending_action(aid)
                    if row and row["status"] != "pending":
                        decision = row["status"]  # decided by another process
                    elif time.monotonic() >= deadline:
                        if await self._db.resolve_pending_action(aid, "timeout"):
                            decision = "timeout"
                        else:  # lost a race with a last-moment decision
                            row = await self._db.get_pending_action(aid)
                            decision = row["status"] if row else "timeout"
        finally:
            self._futures.pop(aid, None)
            if not future.done():
                future.cancel()

        await self._db.resolve_pending_action(aid, decision)  # no-op unless still pending
        self._bus.publish("approvals", {"type": "decided", "id": aid, "decision": decision})
        return decision

    async def decide(self, action_id: str, approve: bool) -> bool:
        """Approve or deny a pending action (from this or another process).

        Returns ``False`` if the action is unknown or was already decided.
        """
        status: Decision = "approved" if approve else "denied"
        won = await self._db.resolve_pending_action(action_id, status)
        future = self._futures.get(action_id)
        if won and future is not None and not future.done():
            future.set_result(status)  # request() publishes the "decided" event
        elif won:
            # The waiting call lives in another process; tell this process's UI anyway.
            self._bus.publish("approvals", {"type": "decided", "id": action_id, "decision": status})
        return won

    def cancel_all(self) -> None:
        for future in list(self._futures.values()):
            if not future.done():
                with contextlib.suppress(asyncio.InvalidStateError):
                    future.set_result("denied")


def _public(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tool": row["tool"],
        "connector": row["connector"],
        "summary": row["summary"],
        "arguments": row["arguments"],
        "created_at": row["created_at"],
    }
