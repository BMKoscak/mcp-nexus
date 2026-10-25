"""Human-in-the-loop gate for tools that send data off the machine.

A tool marked ``requires_approval=True`` calls :meth:`ApprovalGate.request`
before it runs. If auto-approve is off, the request is published on the
``approvals`` topic (the UI shows an Approve / Deny card) and the tool call
waits on a future until someone decides or the timeout expires (→ denied).

The gate sits inside the MCP server, so it applies equally to the web chat
and to external MCP clients such as Claude Desktop.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from nexus.db import Database
from nexus.events import EventBus

Decision = Literal["approved", "denied", "timeout", "auto"]
SETTING_KEY = "auto_approve_outbound"


@dataclass
class PendingAction:
    id: str
    tool: str
    connector: str
    summary: str
    arguments: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future[Decision] = field(default=None, repr=False)  # type: ignore[assignment]

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tool": self.tool,
            "connector": self.connector,
            "summary": self.summary,
            "arguments": self.arguments,
            "created_at": self.created_at,
        }


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
        self._pending: dict[str, PendingAction] = {}

    async def auto_approve(self) -> bool:
        return bool(await self._db.get_setting(SETTING_KEY, self._default_auto))

    async def set_auto_approve(self, value: bool) -> None:
        await self._db.set_setting(SETTING_KEY, bool(value))
        self._bus.publish("approvals", {"type": "auto_approve", "value": bool(value)})

    def pending(self) -> list[dict[str, Any]]:
        return [p.public() for p in self._pending.values()]

    async def request(
        self, *, tool: str, connector: str, summary: str, arguments: dict[str, Any]
    ) -> Decision:
        action = PendingAction(
            id=uuid.uuid4().hex[:12],
            tool=tool,
            connector=connector,
            summary=summary,
            arguments=arguments,
        )
        await self._db.add_pending_action(
            action.id, tool=tool, connector=connector, summary=summary, arguments=arguments
        )

        if await self.auto_approve():
            await self._db.decide_pending_action(action.id, "auto")
            self._bus.publish("approvals", {"type": "auto", **action.public()})
            return "auto"

        action.future = asyncio.get_running_loop().create_future()
        self._pending[action.id] = action
        self._bus.publish("approvals", {"type": "requested", **action.public()})
        try:
            decision = await asyncio.wait_for(action.future, timeout=self.timeout_seconds)
        except TimeoutError:
            decision = "timeout"
        finally:
            self._pending.pop(action.id, None)

        await self._db.decide_pending_action(action.id, decision)
        self._bus.publish("approvals", {"type": "decided", "id": action.id, "decision": decision})
        return decision

    def decide(self, action_id: str, approve: bool) -> bool:
        """Resolve a pending action. Returns ``False`` if it is unknown or already decided."""
        action = self._pending.get(action_id)
        if action is None or action.future.done():
            return False
        action.future.set_result("approved" if approve else "denied")
        return True

    def cancel_all(self) -> None:
        for action in list(self._pending.values()):
            if not action.future.done():
                action.future.set_result("denied")
