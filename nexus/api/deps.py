"""Shared route helpers."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import Request

if TYPE_CHECKING:
    from nexus.events import EventBus
    from nexus.main import Nexus


def get_nexus(request: Request) -> Nexus:
    return request.app.state.nexus


def sse(event: str, data: Any) -> dict[str, str]:
    """Format one sse-starlette event."""
    return {"event": event, "data": json.dumps(data, default=str)}


async def merged_subscription(bus: EventBus, topics: list[str]) -> AsyncIterator[tuple[str, Any]]:
    """Yield ``(topic, event)`` from several bus topics as they arrive."""
    async with contextlib.AsyncExitStack() as stack:
        queues = {topic: await stack.enter_async_context(bus.subscribe(topic)) for topic in topics}
        getters: dict[asyncio.Task[Any], str] = {}
        try:
            for topic, q in queues.items():
                getters[asyncio.create_task(q.get())] = topic
            while True:
                done, _ = await asyncio.wait(getters, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    topic = getters.pop(task)
                    yield topic, task.result()
                    getters[asyncio.create_task(queues[topic].get())] = topic
        finally:
            for task in getters:
                task.cancel()
