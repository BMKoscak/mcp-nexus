"""MCP engine: registration, the RPC tap, plugins, enable/disable, config, approval gating."""

from __future__ import annotations

import asyncio
import json
import textwrap

import httpx

from nexus.mcp_engine import MCPEngine
from tests.conftest import text_of

DICE_PLUGIN = textwrap.dedent(
    '''
    from nexus.connectors import Connector, tool

    class Dice(Connector):
        name = "dice"
        description = "Roll dice"

        @tool()
        async def roll(self, sides: int = 6) -> int:
            """Roll one die with the given number of sides."""
            import random
            return random.randint(1, sides)
    '''
)


async def test_builtin_connectors_load(engine):
    status = {r["name"]: r["status"] for r in engine.registry.public()}
    assert status == {
        name: "active" for name in ["filesystem", "outbound", "web", "system", "sqlite"]
    }
    names = {t.name for t in await engine.list_tools()}
    assert {
        "filesystem_read_file",
        "outbound_send_discord",
        "web_fetch",
        "system_stats",
        "sqlite_query",
    } <= names


async def test_tool_metadata_marks_approval(engine):
    tools = {t.name: t for t in await engine.list_tools()}
    assert tools["outbound_send_discord"].meta["nexus/requiresApproval"] is True
    assert tools["filesystem_read_file"].meta["nexus/requiresApproval"] is False
    assert tools["filesystem_read_file"].annotations.read_only_hint is True
    schema = tools["filesystem_write_file"].input_schema
    assert schema["required"] == ["path", "content"]
    assert set(schema["properties"]["mode"]["enum"]) == {"create", "overwrite", "append"}


async def test_rpc_tap_pairs_requests_and_responses(engine):
    async with engine.bus.subscribe("rpc") as q:
        await engine.call_tool("system_stats", {})
        request = await asyncio.wait_for(q.get(), 2)
        response = await asyncio.wait_for(q.get(), 5)
    assert request["kind"] == "request" and request["method"] == "tools/call"
    assert request["direction"] == "c2s" and request["payload"]["jsonrpc"] == "2.0"
    assert response["kind"] == "response" and response["direction"] == "s2c"
    assert response["rpc_id"] == request["rpc_id"] and response["method"] == "tools/call"
    assert response["latency_ms"] is not None
    await engine.tap.flush()
    rows = await engine.db.list_rpc_entries()
    assert any(r["method"] == "tools/call" for r in rows)


async def test_disable_and_enable_connector(engine):
    await engine.registry.set_enabled("system", False)
    assert "system_stats" not in {t.name for t in await engine.list_tools()}
    assert (await engine.db.get_connector_state("system"))["enabled"] is False
    await engine.registry.set_enabled("system", True)
    assert "system_stats" in {t.name for t in await engine.list_tools()}


async def test_disabled_state_survives_restart(settings, http):
    async with MCPEngine(settings, http_transport=httpx.MockTransport(http)) as eng:
        await eng.registry.set_enabled("web", False)
    async with MCPEngine(settings, http_transport=httpx.MockTransport(http)) as eng:
        assert eng.registry.records["web"].status == "disabled"
        assert "web_fetch" not in {t.name for t in await eng.list_tools()}


async def test_plugin_discovery_and_reload(settings, http):
    settings.plugins_dir.mkdir(parents=True, exist_ok=True)
    (settings.plugins_dir / "dice.py").write_text(DICE_PLUGIN)
    (settings.plugins_dir / "broken.py").write_text("raise RuntimeError('boom')\n")
    (settings.plugins_dir / "_private.py").write_text("raise RuntimeError('should be skipped')\n")
    async with MCPEngine(settings, http_transport=httpx.MockTransport(http)) as eng:
        recs = eng.registry.records
        assert recs["dice"].status == "active" and recs["dice"].source == "plugin"
        assert recs["broken"].status == "error" and "boom" in recs["broken"].error
        assert "_private" not in recs
        result = await eng.call_tool("dice_roll", {"sides": 3})
        assert 1 <= int(text_of(result)) <= 3

        (settings.plugins_dir / "dice.py").unlink()
        await eng.registry.reload_plugins()
        assert "dice" not in eng.registry.records
        assert "dice_roll" not in {t.name for t in await eng.list_tools()}
        assert eng.registry.records["filesystem"].status == "active"  # built-ins untouched


async def test_config_update_encrypts_secrets(engine):
    await engine.registry.update_config(
        "outbound", {"ntfy_topic": "nexus-test", "ntfy_token": "tk_123"}
    )
    stored = (await engine.db.get_connector_state("outbound"))["config"]
    assert stored["ntfy_topic"] == "nexus-test"
    assert "tk_123" not in json.dumps(stored)
    view = engine.registry.config_view("outbound")
    assert view["values"]["ntfy_token"] == "••••••••"
    assert view["values"]["ntfy_topic"] == "nexus-test"
    instance = engine.registry.records["outbound"].instance
    assert instance.config.ntfy_token.get_secret_value() == "tk_123"
    # masked value posted back = unchanged
    await engine.registry.update_config("outbound", {"ntfy_token": "••••••••"})
    assert (
        engine.registry.records["outbound"].instance.config.ntfy_token.get_secret_value()
        == "tk_123"
    )


async def test_gated_tool_waits_for_approval(engine, http):
    await engine.db.upsert_webhook_target(
        "team", kind="discord", url_enc=engine.crypto.encrypt("https://discord.test/hook")
    )
    async with engine.bus.subscribe("approvals") as approvals:
        call = asyncio.create_task(
            engine.call_tool("outbound_send_discord", {"content": "hello", "target": "team"})
        )
        event = await asyncio.wait_for(approvals.get(), 2)
        assert event["type"] == "requested" and "hello" in event["summary"]
        assert "discord.test" not in json.dumps(event)  # the URL never leaves the server
        assert http.requests == []  # nothing sent yet
        engine.approvals.decide(event["id"], approve=True)
        result = await asyncio.wait_for(call, 5)
    assert not result.is_error, text_of(result)
    assert len(http.requests) == 1
    assert json.loads(http.requests[0].content) == {"content": "hello"}


async def test_denied_tool_reports_error_and_sends_nothing(engine, http):
    await engine.db.upsert_webhook_target(
        "team", kind="discord", url_enc=engine.crypto.encrypt("https://discord.test/hook")
    )
    async with engine.bus.subscribe("approvals") as approvals:
        call = asyncio.create_task(
            engine.call_tool("outbound_send_discord", {"content": "x", "target": "team"})
        )
        event = await asyncio.wait_for(approvals.get(), 2)
        engine.approvals.decide(event["id"], approve=False)
        result = await asyncio.wait_for(call, 5)
    assert result.is_error and "denied" in text_of(result)
    assert http.requests == []


async def test_auto_approve_sends_immediately(engine, http):
    await engine.approvals.set_auto_approve(True)
    result = await engine.call_tool(
        "outbound_send_webhook", {"url": "https://hooks.test/x", "payload": {"a": 1}}
    )
    assert not result.is_error, text_of(result)
    assert str(http.requests[0].url) == "https://hooks.test/x"
