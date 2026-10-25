"""Outbound approvals ("Send Stuff" confirmation toggle)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from nexus.api.deps import get_nexus

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class Decision(BaseModel):
    approve: bool


class AutoApprove(BaseModel):
    value: bool


@router.get("")
async def overview(nexus=Depends(get_nexus)) -> dict[str, Any]:
    gate = nexus.engine.approvals
    return {
        "auto_approve": await gate.auto_approve(),
        "timeout_seconds": gate.timeout_seconds,
        "pending": await gate.pending(),
        "history": await gate.history(),
    }


@router.post("/{action_id}")
async def decide(action_id: str, body: Decision, nexus=Depends(get_nexus)) -> dict[str, Any]:
    if not await nexus.engine.approvals.decide(action_id, body.approve):
        raise HTTPException(409, "Action is unknown or was already decided.")
    return {"id": action_id, "decision": "approved" if body.approve else "denied"}


@router.put("/auto")
async def set_auto(body: AutoApprove, nexus=Depends(get_nexus)) -> dict[str, bool]:
    await nexus.engine.approvals.set_auto_approve(body.value)
    return {"auto_approve": body.value}
