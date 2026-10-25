"""db, crypto, events and the approval gate."""

from __future__ import annotations

import asyncio

import pytest
from cryptography.fernet import Fernet

from nexus.approvals import ApprovalGate
from nexus.crypto import SecretBox, mask
from nexus.db import MIGRATIONS, Database
from nexus.events import EventBus


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "t.db")
    await database.connect()
    yield database
    await database.close()


async def test_migrations_are_idempotent(db):
    assert await db.schema_version() == len(MIGRATIONS)
    await db.migrate()
    assert await db.schema_version() == len(MIGRATIONS)


async def test_settings_roundtrip(db):
    assert await db.get_setting("missing", 42) == 42
    await db.set_setting("k", {"a": [1, 2]})
    await db.set_setting("k", {"a": [3]})
    assert await db.get_setting("k") == {"a": [3]}


async def test_provider_upsert_keeps_existing_key(db):
    await db.upsert_provider("openai", api_key_enc="ENC1")
    await db.upsert_provider("openai", base_url="http://x")  # no key → keep old one
    row = await db.get_provider("openai")
    assert row["api_key_enc"] == "ENC1" and row["base_url"] == "http://x"


async def test_conversation_messages(db):
    cid = await db.create_conversation("hello", model="ollama/llama3.2")
    await db.add_message(cid, "user", "hi")
    await db.add_message(
        cid, "assistant", None, tool_calls=[{"id": "1", "name": "x", "arguments": {}}]
    )
    msgs = await db.list_messages(cid)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[1]["tool_calls"][0]["name"] == "x"
    await db.delete_conversation(cid)
    assert await db.list_messages(cid) == []


async def test_rpc_log_is_trimmed(db):
    entries = [
        {
            "ts": i,
            "transport": "inproc",
            "direction": "c2s",
            "kind": "request",
            "method": "m",
            "rpc_id": i,
            "payload": {},
        }
        for i in range(30)
    ]
    await db.add_rpc_entries(entries, keep=10)
    rows = await db.list_rpc_entries()
    assert len(rows) == 10 and rows[-1]["rpc_id"] == "29"


def test_crypto_roundtrip_and_key_file(settings):
    box = SecretBox.from_settings(settings)
    token = box.encrypt("sk-secret")
    assert token != "sk-secret" and box.decrypt(token) == "sk-secret"
    assert (settings.data_dir / "secret.key").exists()
    assert SecretBox.from_settings(settings).decrypt(token) == "sk-secret"  # same key reloaded
    assert SecretBox(Fernet.generate_key()).decrypt(token) is None  # wrong key → None, no crash


def test_mask():
    assert mask("sk-ant-1234567890abcd") == "sk-ant…abcd"
    assert mask("short") == "•••••"
    assert mask(None) == ""


async def test_event_bus_drops_oldest_when_full():
    bus = EventBus(queue_size=2)
    async with bus.subscribe("t") as q:
        for i in range(5):
            bus.publish("t", {"i": i})
        assert [q.get_nowait()["i"], q.get_nowait()["i"]] == [3, 4]
    assert bus.subscriber_count("t") == 0


async def test_approval_gate_flow(db):
    bus = EventBus()
    gate = ApprovalGate(db, bus, timeout_seconds=1)

    async with bus.subscribe("approvals") as events:
        task = asyncio.create_task(
            gate.request(tool="t", connector="c", summary="s", arguments={"x": 1})
        )
        requested = await asyncio.wait_for(events.get(), 1)
        assert requested["type"] == "requested"
        assert await gate.decide(requested["id"], approve=True)
        assert await task == "approved"
        assert not await gate.decide(requested["id"], approve=True)  # already decided

    denied = asyncio.create_task(gate.request(tool="t", connector="c", summary="s", arguments={}))
    await asyncio.sleep(0.05)
    await gate.decide((await gate.pending())[0]["id"], approve=False)
    assert await denied == "denied"

    assert await gate.request(tool="t", connector="c", summary="s", arguments={}) == "timeout"

    await gate.set_auto_approve(True)
    assert await gate.request(tool="t", connector="c", summary="s", arguments={}) == "auto"

    statuses = sorted(r["status"] for r in await db.list_pending_actions())
    assert statuses == ["approved", "auto", "denied", "timeout"]


async def test_approval_decided_from_another_process(tmp_path):
    """The stdio server and the web UI are separate processes sharing one SQLite file."""
    db_a, db_b = Database(tmp_path / "shared.db"), Database(tmp_path / "shared.db")
    await db_a.connect()
    await db_b.connect()
    try:
        waiting = ApprovalGate(db_a, EventBus(), timeout_seconds=5)  # e.g. `nexus stdio`
        ui = ApprovalGate(db_b, EventBus(), timeout_seconds=5)  # e.g. `nexus serve`
        call = asyncio.create_task(
            waiting.request(tool="t", connector="c", summary="s", arguments={})
        )
        for _ in range(50):
            pending = await ui.pending()
            if pending:
                break
            await asyncio.sleep(0.02)
        assert await ui.decide(pending[0]["id"], approve=True)
        assert await asyncio.wait_for(call, 3) == "approved"
    finally:
        await db_a.close()
        await db_b.close()
