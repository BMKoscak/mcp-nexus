"""End-to-end smoke test against a *running* MCP Nexus server.

    nexus serve                         # in one terminal
    python scripts/smoke_test.py        # in another (default http://127.0.0.1:8765)
    python scripts/smoke_test.py http://127.0.0.1:9000

Exercises every REST route, the HTMX fragments, the SSE event stream, the
Streamable HTTP MCP endpoint and (if a model is configured) one chat turn.
It creates and then removes a conversation, a destination and a template.
Exit code is non-zero if anything fails.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import httpx
from mcp.client import Client

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765").rstrip("/")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, note: str = "") -> None:
    results.append((name, ok, note))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{('  — ' + note) if note else ''}")


async def expect(
    c: httpx.AsyncClient, method: str, path: str, status: int = 200, **kw
) -> httpx.Response:
    r = await c.request(method, path, **kw)
    check(
        f"{method} {path}",
        r.status_code == status,
        f"HTTP {r.status_code}" if r.status_code != status else "",
    )
    return r


async def sse_events(c: httpx.AsyncClient, path: str, body: dict) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    async with c.stream("POST", path, json=body, timeout=120) as r:
        name = None
        async for line in r.aiter_lines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:") and name:
                events.append((name, json.loads(line[5:].strip())))
                name = None
    return events


async def main() -> int:
    print(f"MCP Nexus smoke test → {BASE}\n")
    async with httpx.AsyncClient(base_url=BASE, timeout=30) as c:
        # system
        health = (await expect(c, "GET", "/api/health")).json()
        check("health reports tools", health.get("tools", 0) > 0, f"{health.get('tools')} tools")
        await expect(c, "GET", "/api/settings")
        await expect(c, "GET", "/api/inspector/history")

        # pages & fragments
        page = await expect(c, "GET", "/")
        check("dashboard markup", 'x-data="nexus"' in page.text)
        await expect(c, "GET", "/static/app.js")
        await expect(c, "GET", "/static/app.css")
        await expect(c, "GET", "/ui/connectors")
        await expect(c, "GET", "/ui/conversations")
        await expect(c, "GET", "/docs")
        await expect(c, "GET", "/openapi.json")

        # LLM hub
        providers = (await expect(c, "GET", "/api/llm/providers")).json()
        check(
            "providers listed", {p["name"] for p in providers} >= {"anthropic", "openai", "ollama"}
        )
        models = (await expect(c, "GET", "/api/llm/models")).json()
        available = [m["id"] for info in models.values() for m in info["models"]]
        check(
            "models discovered",
            True,
            f"{len(available)} available" + ("" if available else " (no provider configured)"),
        )
        active = (await expect(c, "GET", "/api/llm/active")).json()["model"]
        await expect(c, "POST", "/api/llm/providers/ollama/test", json={})

        # connectors & raw MCP surface
        await expect(c, "GET", "/api/connectors")
        await expect(c, "GET", "/api/connectors/filesystem/config")
        await expect(c, "POST", "/api/connectors/system/disable")
        tools = {t["name"] for t in (await expect(c, "GET", "/api/tools")).json()}
        check("disabled connector hides its tools", "system_stats" not in tools)
        await expect(c, "POST", "/api/connectors/system/enable")
        r = (await expect(c, "POST", "/api/tools/system_stats/call", json={"arguments": {}})).json()
        check("system_stats returns data", not r["is_error"])
        r = (
            await expect(
                c,
                "POST",
                "/api/tools/filesystem_write_file/call",
                json={
                    "arguments": {"path": "smoke-test.txt", "content": "ok", "mode": "overwrite"}
                },
            )
        ).json()
        check("filesystem write inside workspace", not r["is_error"])
        r = (
            await expect(
                c,
                "POST",
                "/api/tools/filesystem_read_file/call",
                json={"arguments": {"path": "../../outside.txt"}},
            )
        ).json()
        check("filesystem refuses path escape", r["is_error"])
        await expect(c, "GET", "/api/resources")
        await expect(c, "GET", "/api/resources/read", params={"uri": "nexus://system/stats"})
        await expect(c, "GET", "/api/prompts")

        # destinations
        await expect(
            c,
            "PUT",
            "/api/targets/smoke-test",
            json={"kind": "http", "url": "https://example.com/hook/SECRET"},
        )
        targets = (await expect(c, "GET", "/api/targets")).json()
        check("destination URL masked", "SECRET" not in json.dumps(targets))
        await expect(c, "DELETE", "/api/targets/smoke-test", status=204)

        # approvals
        await expect(c, "GET", "/api/approvals")
        await expect(c, "POST", "/api/approvals/not-a-real-id", status=409, json={"approve": False})

        # templates
        await expect(c, "GET", "/api/templates")
        await expect(c, "GET", "/api/templates/summarize-url")
        v = (
            await expect(c, "POST", "/api/templates/validate", json={"body": "Hi {{ name }}"})
        ).json()
        check("template variables detected", v.get("variables") == ["name"])
        await expect(
            c,
            "PUT",
            "/api/templates/smoke-test",
            json={"name": "smoke-test", "body": "Say hi to {{ name }}.", "description": "smoke"},
        )
        r = (
            await expect(
                c, "POST", "/api/templates/smoke-test/render", json={"arguments": {"name": "Ben"}}
            )
        ).json()
        check("template renders via MCP prompts/get", r.get("text") == "Say hi to Ben.")
        await expect(c, "DELETE", "/api/templates/smoke-test", status=204)

        # conversations
        convo = (
            await expect(c, "POST", "/api/conversations", status=201, json={"title": "smoke"})
        ).json()
        await expect(c, "GET", f"/api/conversations/{convo['id']}")
        await expect(c, "GET", "/api/conversations")
        await expect(c, "DELETE", f"/api/conversations/{convo['id']}", status=204)

        # live event stream: open it, trigger a tool call, expect the rpc event
        got: list[str] = []

        async def listen() -> None:
            async with c.stream("GET", "/api/events?topics=rpc", timeout=15) as r:
                async for line in r.aiter_lines():
                    if line.startswith("data:") and "tools/call" in line:
                        got.append(line)
                        return

        listener = asyncio.create_task(listen())
        await asyncio.sleep(0.5)
        await c.post("/api/tools/system_stats/call", json={"arguments": {}})
        try:
            await asyncio.wait_for(listener, 10)
        except TimeoutError:
            listener.cancel()
        check("SSE /api/events streams rpc traffic", bool(got))

        # chat (only meaningful with a configured model)
        if active in available:
            t0 = time.perf_counter()
            events = await sse_events(
                c, "/api/chat", {"message": "Reply with the single word: pong", "use_tools": False}
            )
            kinds = [e[0] for e in events]
            check(
                f"chat with {active}",
                "done" in kinds,
                f"{time.perf_counter() - t0:.1f}s, events: {kinds[:6]}",
            )
            cid = next((d["id"] for k, d in events if k == "conversation"), None)
            if cid:
                await c.delete(f"/api/conversations/{cid}")
        else:
            events = await sse_events(c, "/api/chat", {"message": "hi"})
            check(
                "chat reports a missing model clearly",
                events and events[-1][0] == "error",
                f"active model {active!r} unavailable — skipped live chat",
            )

    # external MCP client over Streamable HTTP
    try:
        async with Client(f"{BASE}/mcp") as client:
            mcp_tools = (await client.list_tools()).tools
            prompts = (await client.list_prompts()).prompts
            res = await client.call_tool("filesystem_list_dir", {})
        check(
            "MCP over HTTP: tools/list, prompts/list, tools/call",
            bool(mcp_tools) and not res.is_error,
            f"{len(mcp_tools)} tools, {len(prompts)} prompts",
        )
    except Exception as exc:  # pragma: no cover - reported as a failure
        check("MCP over HTTP", False, repr(exc)[:200])

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
