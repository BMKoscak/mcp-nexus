from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path

import httpx
import pytest

from nexus.config import Settings
from nexus.mcp_engine import MCPEngine


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        workspace_dir=tmp_path / "workspace",
        plugins_dir=tmp_path / "plugins",
        approval_timeout_seconds=5,
    )


class HttpRecorder:
    """Collects outgoing requests and answers them with a configurable handler."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(
            200, text="ok"
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def http() -> HttpRecorder:
    return HttpRecorder()


@pytest.fixture
async def engine(settings: Settings, http: HttpRecorder) -> AsyncIterator[MCPEngine]:
    eng = MCPEngine(settings, http_transport=httpx.MockTransport(http))
    await eng.start()
    try:
        yield eng
    finally:
        await eng.stop()


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)
