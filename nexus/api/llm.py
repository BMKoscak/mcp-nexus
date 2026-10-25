"""LLM Hub: provider keys, base URLs, models, connection tests, active model."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from nexus.api.deps import get_nexus

router = APIRouter(prefix="/api/llm", tags=["llm"])


class ProviderUpdate(BaseModel):
    api_key: str | None = None
    base_url: str | None = None
    enabled: bool | None = None
    clear_key: bool = False


class TestRequest(BaseModel):
    model: str | None = None


class ActiveModel(BaseModel):
    model: str


@router.get("/providers")
async def list_providers(nexus=Depends(get_nexus)) -> list[dict[str, Any]]:
    return await nexus.router.list_providers()


@router.put("/providers/{name}")
async def update_provider(name: str, body: ProviderUpdate, nexus=Depends(get_nexus)) -> dict:
    await nexus.router.configure(
        name,
        api_key=body.api_key,
        base_url=body.base_url,
        enabled=body.enabled,
        clear_key=body.clear_key,
    )
    return next(p for p in await nexus.router.list_providers() if p["name"] == name)


@router.post("/providers/{name}/test")
async def test_provider(
    name: str, body: TestRequest | None = None, nexus=Depends(get_nexus)
) -> dict:
    model = body.model if body else None
    if model and "/" in model:
        _, model = nexus.router.split(model)
    return await nexus.router.test_connection(name, model)


@router.get("/models")
async def list_models(provider: str | None = None, nexus=Depends(get_nexus)) -> dict[str, Any]:
    return await nexus.router.list_models(provider)


@router.get("/active")
async def get_active(nexus=Depends(get_nexus)) -> dict[str, str]:
    return {"model": await nexus.router.active_model()}


@router.put("/active")
async def set_active(body: ActiveModel, nexus=Depends(get_nexus)) -> dict[str, str]:
    await nexus.router.set_active_model(body.model)
    return {"model": body.model}
