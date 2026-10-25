"""End-to-end API tests with a scripted fake LLM provider."""

from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import httpx
import pytest
import uvicorn
from mcp.client import Client

from nexus.main import create_app
from nexus.providers import Done, ModelInfo, Provider, TextDelta, ToolCall, ToolCallEvent
from tests.conftest import HttpRecorder


class FakeProvider(Provider):
    """Replays ``script``: one list of events per LLM call. Records what it was sent."""

    name = "fake"
    label = "Fake"
    needs_api_key = False
    script: ClassVar[list[list[Any]]] = []
    calls: ClassVar[list[dict[str, Any]]] = []

    async def list_models(self) -> list[ModelInfo]:
        return [ModelInfo("echo")]

    async def stream(self, model, messages, tools=None, **kw) -> AsyncIterator[Any]:
        FakeProvider.calls.append({"model": model, "messages": list(messages), "tools": tools})
        for ev in (
            FakeProvider.script.pop(0)
            if FakeProvider.script
            else [TextDelta("ok"), Done("end_turn")]
        ):
            yield ev


@pytest.fixture
async def app(settings, http: HttpRecorder):
    FakeProvider.script, FakeProvider.calls = [], []
    application = create_app(settings, http_transport=httpx.MockTransport(http))
    application.state.nexus.router.register("fake", FakeProvider)
    # Like uvicorn, run the whole lifespan inside one task (anyio scopes are task-bound).
    started, stop = asyncio.Event(), asyncio.Event()

    async def lifespan_task() -> None:
        async with application.router.lifespan_context(application):
            started.set()
            await stop.wait()

    task = asyncio.create_task(lifespan_task())
    await asyncio.wait_for(started.wait(), 10)
    await application.state.nexus.router.set_active_model("fake/echo")
    try:
        yield application
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


@pytest.fixture
async def api(app) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as c:
        yield c


def parse_sse(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        name, data = None, []
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if name and data:
            events.append((name, json.loads("\n".join(data))))
    return events


# ── basics ───────────────────────────────────────────────────
async def test_health_and_settings(api):
    health = (await api.get("/api/health")).json()
    assert health["status"] == "ok" and health["tools"] >= 21
    assert health["connectors"]["filesystem"] == "active"
    settings = (await api.get("/api/settings")).json()
    assert settings["mcp_endpoint"].endswith("/mcp") and settings["auto_approve_outbound"] is False


async def test_index_page(api):
    r = await api.get("/")
    assert r.status_code == 200 and "MCP Nexus" in r.text


# ── LLM hub ──────────────────────────────────────────────────
async def test_provider_key_is_encrypted_and_masked(api, app):
    r = await api.put("/api/llm/providers/openai", json={"api_key": "sk-proj-abcdefghijklmnop1234"})
    assert r.status_code == 200
    assert r.json()["api_key_masked"] == "sk-pro…1234" and r.json()["api_key_source"] == "ui"
    row = await app.state.nexus.engine.db.get_provider("openai")
    assert "abcdefghijklmnop" not in row["api_key_enc"]
    listed = {p["name"]: p for p in (await api.get("/api/llm/providers")).json()}
    assert listed["openai"]["configured"] and not listed["anthropic"]["configured"]
    assert "abcdefghijklmnop" not in json.dumps(listed)
    r = await api.put("/api/llm/providers/openai", json={"clear_key": True})
    assert r.json()["configured"] is False


async def test_models_and_active_model(api):
    models = (await api.get("/api/llm/models")).json()
    assert models["fake"]["models"] == [{"id": "fake/echo", "name": "echo"}]
    assert models["anthropic"]["error"] == "no API key"
    assert (await api.put("/api/llm/active", json={"model": "fake/echo"})).json() == {
        "model": "fake/echo"
    }
    assert (await api.put("/api/llm/active", json={"model": "no-slash"})).status_code == 502
    test = (await api.post("/api/llm/providers/fake/test", json={"model": "fake/echo"})).json()
    assert test["ok"] and test["reply"] == "ok"


# ── chat / agent loop ────────────────────────────────────────
async def test_chat_runs_tool_loop_and_persists(api, app):
    FakeProvider.script = [
        [
            TextDelta("Writing it."),
            ToolCallEvent(
                ToolCall("c1", "filesystem_write_file", {"path": "hi.txt", "content": "hello"})
            ),
            Done("tool_use"),
        ],
        [TextDelta("Done — saved hi.txt."), Done("end_turn", {"output_tokens": 5})],
    ]
    r = await api.post("/api/chat", json={"message": "save hello to hi.txt"})
    events = parse_sse(r.text)
    kinds = [e[0] for e in events]
    assert kinds == ["conversation", "text", "tool_call", "tool_result", "text", "done"]
    result = dict(events)["tool_result"]
    assert result["is_error"] is False and "hi.txt" in result["content"]
    assert (app.state.nexus.settings.workspace_dir / "hi.txt").read_text() == "hello"

    # second LLM call received the tool result in its history, and the MCP tools as ToolDefs
    second = FakeProvider.calls[1]
    assert second["messages"][-1].role == "tool" and second["messages"][-1].tool_call_id == "c1"
    assert any(t.name == "filesystem_write_file" for t in second["tools"])

    cid = dict(events)["conversation"]["id"]
    convo = (await api.get(f"/api/conversations/{cid}")).json()
    assert convo["title"] == "save hello to hi.txt"
    assert [m["role"] for m in convo["messages"]] == ["user", "assistant", "tool", "assistant"]

    # every step went through MCP, so the Inspector has the tools/call
    history = (await api.get("/api/inspector/history")).json()
    assert any(h["method"] == "tools/call" and h["kind"] == "response" for h in history)


async def test_chat_continues_conversation(api):
    first = parse_sse((await api.post("/api/chat", json={"message": "one"})).text)
    cid = first[0][1]["id"]
    await api.post("/api/chat", json={"message": "two", "conversation_id": cid})
    assert [m.content for m in FakeProvider.calls[1]["messages"]] == ["one", "ok", "two"]


async def test_chat_reports_unknown_provider(api):
    events = parse_sse((await api.post("/api/chat", json={"message": "x", "model": "nope/m"})).text)
    assert events == [("error", {"type": "error", "message": events[0][1]["message"]})]
    assert "Unknown provider" in events[0][1]["message"]


async def test_chat_with_approval(api, app, http):
    await api.put("/api/targets/team", json={"kind": "discord", "url": "https://discord.test/hook"})
    FakeProvider.script = [
        [
            ToolCallEvent(
                ToolCall("c1", "outbound_send_discord", {"content": "ship it", "target": "team"})
            ),
            Done("tool_use"),
        ],
        [TextDelta("Sent."), Done("end_turn")],
    ]

    async def approve_when_asked():
        for _ in range(100):
            pending = (await api.get("/api/approvals")).json()["pending"]
            if pending:
                assert pending[0]["summary"] == "Discord (team): ship it"
                r = await api.post(f"/api/approvals/{pending[0]['id']}", json={"approve": True})
                assert r.status_code == 200
                return
            await asyncio.sleep(0.05)
        raise AssertionError("approval never requested")

    approver = asyncio.create_task(approve_when_asked())
    events = parse_sse((await api.post("/api/chat", json={"message": "tell the team"})).text)
    await approver
    assert dict(events)["tool_result"]["is_error"] is False
    assert json.loads(http.requests[-1].content) == {"content": "ship it"}
    history = (await api.get("/api/approvals")).json()["history"]
    assert history[0]["status"] == "approved"


async def test_approval_double_decision_conflicts(api):
    r = await api.post("/api/approvals/doesnotexist", json={"approve": True})
    assert r.status_code == 409
    assert (await api.put("/api/approvals/auto", json={"value": True})).json() == {
        "auto_approve": True
    }


# ── connectors / tools ───────────────────────────────────────
async def test_connector_toggle_and_config(api):
    assert (await api.post("/api/connectors/system/disable")).json()["status"] == "disabled"
    assert "system_stats" not in {t["name"] for t in (await api.get("/api/tools")).json()}
    await api.post("/api/connectors/system/enable")
    cfg = (await api.get("/api/connectors/filesystem/config")).json()
    assert "allow_write" in cfg["schema"]["properties"]
    bad = await api.put("/api/connectors/filesystem/config", json={"max_read_bytes": -5})
    assert bad.status_code == 422
    assert (await api.get("/api/connectors/nope/config")).status_code == 404


async def test_manual_tool_call_and_resources(api):
    r = (await api.post("/api/tools/filesystem_list_dir/call", json={"arguments": {}})).json()
    assert r["is_error"] is False
    tools = {t["name"]: t for t in (await api.get("/api/tools")).json()}
    assert tools["outbound_send_email"]["requires_approval"] is True
    assert tools["filesystem_read_file"]["connector"] == "filesystem"
    uris = [r["uri"] for r in (await api.get("/api/resources")).json()]
    assert "nexus://system/stats" in uris
    content = (
        await api.get("/api/resources/read", params={"uri": "nexus://filesystem/tree"})
    ).json()
    assert content["contents"][0]["text"]


async def test_targets_never_expose_urls(api):
    await api.put(
        "/api/targets/ops",
        json={"kind": "slack", "url": "https://hooks.slack.com/services/T0/B0/SECRET123"},
    )
    listed = (await api.get("/api/targets")).json()
    assert listed[0]["name"] == "ops" and "SECRET123" not in json.dumps(listed)
    assert (await api.put("/api/targets/Bad Name", json={"url": "https://x"})).status_code == 400


# ── templates ────────────────────────────────────────────────
async def test_starter_templates_are_mcp_prompts(api):
    names = {t["name"] for t in (await api.get("/api/templates")).json()}
    assert {"summarize-url", "system-health-report", "save-research-notes"} <= names
    prompts = {p["name"] for p in (await api.get("/api/prompts")).json()}
    assert names <= prompts


async def test_template_crud_render_and_run(api):
    body = {
        "name": "greet",
        "description": "Say hi",
        "body": "Say hi to {{ who }} in {{ lang | default('English') }}.",
    }
    saved = (await api.put("/api/templates/greet", json=body)).json()
    assert [a["name"] for a in saved["arguments"]] == ["lang", "who"]
    v = (
        await api.post("/api/templates/validate", json={"body": "{{ a }} {% if b %}x{% endif %}"})
    ).json()
    assert v == {"ok": True, "variables": ["a", "b"]}
    assert (await api.post("/api/templates/validate", json={"body": "{{ oops"})).json()[
        "ok"
    ] is False

    # inferred args are required; make lang optional and render via MCP prompts/get
    body["arguments"] = [{"name": "who"}, {"name": "lang", "required": False}]
    await api.put("/api/templates/greet", json=body)
    r = (await api.post("/api/templates/greet/render", json={"arguments": {"who": "Ben"}})).json()
    assert r["text"] == "Say hi to Ben in English."
    missing = await api.post("/api/templates/greet/render", json={"arguments": {}})
    assert missing.status_code == 400 and "who" in missing.json()["detail"]

    events = parse_sse(
        (
            await api.post(
                "/api/templates/greet/run", json={"arguments": {"who": "Ana", "lang": "Slovene"}}
            )
        ).text
    )
    assert events[0] == ("prompt", {"text": "Say hi to Ana in Slovene.", "template": "greet"})
    assert FakeProvider.calls[-1]["messages"][-1].content == "Say hi to Ana in Slovene."

    await api.delete("/api/templates/greet")
    assert "greet" not in {p["name"] for p in (await api.get("/api/prompts")).json()}


async def test_template_sandbox_blocks_python_internals(api):
    body = {
        "name": "evil",
        "body": "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        "arguments": [],
    }
    await api.put("/api/templates/evil", json=body)
    r = await api.post("/api/templates/evil/render", json={"arguments": {}})
    assert r.status_code == 400


# ── external MCP clients over Streamable HTTP ────────────────
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_mcp_http_endpoint_for_external_clients(settings, http):
    port = _free_port()
    settings.port = port
    application = create_app(settings, http_transport=httpx.MockTransport(http))
    server = uvicorn.Server(
        uvicorn.Config(application, host="127.0.0.1", port=port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.05)
        async with Client(f"http://127.0.0.1:{port}/mcp") as client:
            tools = {t.name for t in (await client.list_tools()).tools}
            assert "filesystem_read_file" in tools
            prompts = {p.name for p in (await client.list_prompts()).prompts}
            assert "summarize-url" in prompts
            result = await client.call_tool("filesystem_list_dir", {})
            assert result.is_error is False

        engine = application.state.nexus.engine
        await engine.tap.flush()
        rows = await engine.db.list_rpc_entries()
        external = [r for r in rows if r["transport"] == "http"]
        assert {r["method"] for r in external} >= {"server/discover", "tools/list", "tools/call"}
        call_response = next(
            r for r in external if r["method"] == "tools/call" and r["kind"] == "response"
        )
        assert (
            call_response["latency_ms"] is not None and call_response["payload"]["jsonrpc"] == "2.0"
        )
        assert all(r["transport"] != "http" for r in rows if r["session"] == "host")
    finally:
        server.should_exit = True
        await task


# ── dashboard + HTMX partials ────────────────────────────────
async def test_dashboard_page_wires_components(api):
    html = (await api.get("/")).text
    for marker in (
        'x-data="nexus"',
        'x-data="chat"',
        'x-data="connections"',
        'x-data="studio"',
        'x-data="inspector"',
        'hx-get="/ui/connectors"',
        "/static/app.js",
    ):
        assert marker in html
    assert (await api.get("/static/app.css")).status_code == 200


async def test_connector_partials_toggle(api):
    html = (await api.get("/ui/connectors")).text
    assert 'id="connector-filesystem"' in html and 'data-status="active"' in html
    off = (await api.post("/ui/connectors/system/toggle")).text
    assert 'data-status="disabled"' in off and 'aria-checked="false"' in off
    on = (await api.post("/ui/connectors/system/toggle")).text
    assert 'data-status="active"' in on


async def test_conversation_partial(api):
    assert "Your chats will be listed here." in (await api.get("/ui/conversations")).text
    events = parse_sse((await api.post("/api/chat", json={"message": "hello there"})).text)
    cid = events[0][1]["id"]
    html = (await api.get(f"/ui/conversations?active={cid}")).text
    assert "hello there" in html and "is-active" in html


async def test_destinations_show_only_the_host(api):
    await api.put(
        "/api/targets/d",
        json={"kind": "discord", "url": "https://discord.com/api/webhooks/1/TOKEN"},
    )
    assert (await api.get("/api/targets")).json()[0]["url_masked"] == "discord.com/…"


async def test_provider_extra_survives_persistence(api):
    """Gemini thought signatures must come back on the next call, even after a DB round trip."""
    FakeProvider.script = [
        [
            ToolCallEvent(ToolCall("c1", "system_stats", {}, {"thoughtSignature": "SIG"})),
            Done("tool_use"),
        ],
        [TextDelta("ok"), Done("end_turn")],
        [TextDelta("again"), Done("end_turn")],
    ]
    events = parse_sse((await api.post("/api/chat", json={"message": "stats"})).text)
    assert "thoughtSignature" not in json.dumps(events)  # never sent to the browser
    assert FakeProvider.calls[1]["messages"][1].tool_calls[0].extra == {"thoughtSignature": "SIG"}
    cid = events[0][1]["id"]
    await api.post(
        "/api/chat", json={"message": "more", "conversation_id": cid}
    )  # history rebuilt from SQLite
    assert FakeProvider.calls[2]["messages"][1].tool_calls[0].extra == {"thoughtSignature": "SIG"}


async def test_gemini_listed_as_provider(api):
    providers = {p["name"]: p for p in (await api.get("/api/llm/providers")).json()}
    assert providers["gemini"]["label"] == "Google Gemini" and providers["gemini"]["needs_api_key"]
    models = (await api.get("/api/llm/models")).json()
    assert models["gemini"]["error"] == "no API key"
