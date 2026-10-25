"""Built-in connector behaviour: sandboxing, SSRF guard, HTML conversion, SQL safety."""

from __future__ import annotations

import os
import sqlite3
import sys

import httpx
import pytest

from nexus.connectors.web_fetch import HTMLParser, html_to_markdown
from nexus.netguard import BlockedAddressError, check_url
from tests.conftest import text_of


# ── filesystem ───────────────────────────────────────────────
async def test_filesystem_write_read_append(engine):
    r = await engine.call_tool("filesystem_write_file", {"path": "a/b.txt", "content": "one"})
    assert not r.is_error
    r = await engine.call_tool("filesystem_write_file", {"path": "a/b.txt", "content": "two"})
    assert r.is_error and "already exists" in text_of(r)
    await engine.call_tool(
        "filesystem_write_file", {"path": "a/b.txt", "content": "+", "mode": "append"}
    )
    assert text_of(await engine.call_tool("filesystem_read_file", {"path": "a/b.txt"})) == "one+"
    hits = await engine.call_tool("filesystem_search", {"pattern": "*.txt", "contains": "one"})
    assert "a/b.txt" in text_of(hits)


@pytest.mark.parametrize(
    "path", ["../outside.txt", "a/../../outside.txt", "/etc/passwd", "C:\\Windows\\win.ini"]
)
async def test_filesystem_rejects_escapes(engine, path):
    r = await engine.call_tool("filesystem_read_file", {"path": path})
    assert r.is_error


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need admin rights on Windows")
async def test_filesystem_rejects_symlink_escape(engine, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("nope")
    os.symlink(secret, engine.settings.workspace_dir / "link.txt")
    r = await engine.call_tool("filesystem_read_file", {"path": "link.txt"})
    assert r.is_error and "outside the workspace" in text_of(r)


async def test_filesystem_write_can_be_disabled(engine):
    await engine.registry.update_config("filesystem", {"allow_write": False})
    r = await engine.call_tool("filesystem_write_file", {"path": "x.txt", "content": "x"})
    assert r.is_error and "disabled" in text_of(r)


# ── netguard / web ───────────────────────────────────────────
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest",
        "http://[::1]/",
        "http://[::ffff:127.0.0.1]/",
        "file:///etc/passwd",
        "ftp://example.com/",
    ],
)
async def test_netguard_blocks(url):
    with pytest.raises(BlockedAddressError):
        await check_url(url)


async def test_netguard_allows_public_ip_and_can_be_disabled():
    await check_url("https://93.184.215.14/")
    await check_url("http://127.0.0.1:8000/", block_private=False)


async def test_web_fetch_markdown(engine, http):
    html = (
        "<html><head><title>Demo</title><script>evil()</script></head><body>"
        "<h1>Hello</h1><p>First <b>para</b>.</p><ul><li>one</li><li>two</li></ul>"
        "<pre>code()</pre></body></html>"
    )
    http.handler = lambda r: httpx.Response(
        200, text=html, headers={"content-type": "text/html; charset=utf-8"}
    )
    r = await engine.call_tool("web_fetch", {"url": "https://93.184.215.14/page"})
    assert not r.is_error, text_of(r)
    data = r.structured_content
    assert data["title"] == "Demo"
    assert data["content"] == "# Hello\n\nFirst para.\n\n- one\n\n- two\n\n```\ncode()\n```"
    assert "evil" not in data["content"]


async def test_web_fetch_blocks_redirect_to_private(engine, http):
    http.handler = lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    r = await engine.call_tool("web_fetch", {"url": "https://93.184.215.14/"})
    assert r.is_error and "non-public" in text_of(r)
    assert len(http.requests) == 1  # never followed


async def test_web_fetch_truncates_large_bodies(engine, http):
    await engine.registry.update_config("web", {"max_bytes": 1000})
    http.handler = lambda r: httpx.Response(
        200, text="x" * 50_000, headers={"content-type": "text/plain"}
    )
    r = await engine.call_tool("web_fetch", {"url": "https://93.184.215.14/big"})
    assert r.structured_content["truncated"] is True
    assert len(r.structured_content["content"]) <= 1000


def test_markdown_ignores_nested_blocks():
    tree = HTMLParser("<body><ul><li><p>nested</p></li></ul></body>")
    assert html_to_markdown(tree) == "- nested"


# ── outbound ─────────────────────────────────────────────────
async def test_outbound_list_targets_hides_urls(engine):
    await engine.db.upsert_webhook_target(
        "ops", kind="slack", url_enc=engine.crypto.encrypt("https://hooks.slack.test/T/X")
    )
    r = await engine.call_tool("outbound_list_targets", {})
    assert "ops" in text_of(r) and "hooks.slack.test" not in text_of(r)


async def test_outbound_unknown_target(engine):
    await engine.approvals.set_auto_approve(True)
    r = await engine.call_tool("outbound_send_slack", {"text": "hi", "target": "nope"})
    assert r.is_error and "Unknown target" in text_of(r)


async def test_outbound_raw_urls_can_be_disabled(engine, http):
    await engine.approvals.set_auto_approve(True)
    await engine.registry.update_config("outbound", {"allow_raw_urls": False})
    r = await engine.call_tool("outbound_send_webhook", {"url": "https://x.test", "payload": "hi"})
    assert r.is_error and http.requests == []


async def test_outbound_notify_uses_ntfy(engine, http):
    await engine.approvals.set_auto_approve(True)
    await engine.registry.update_config("outbound", {"ntfy_topic": "my-topic"})
    r = await engine.call_tool(
        "outbound_notify", {"message": "Build done", "title": "CI", "priority": 4}
    )
    assert not r.is_error, text_of(r)
    req = http.requests[0]
    assert str(req.url) == "https://ntfy.sh/my-topic"
    assert req.headers["Title"] == "CI" and req.content == b"Build done"


# ── sqlite ───────────────────────────────────────────────────
@pytest.fixture
def sample_db(settings):
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    path = settings.workspace_dir / "shop.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE items(id INTEGER PRIMARY KEY, name TEXT NOT NULL, price REAL)")
        conn.executemany(
            "INSERT INTO items(name, price) VALUES(?, ?)", [("apple", 1.2), ("pear", 0.8)]
        )
    return path


async def test_sqlite_read_only_queries(engine, sample_db):
    assert "shop.db" in text_of(await engine.call_tool("sqlite_list_databases", {}))
    r = await engine.call_tool(
        "sqlite_query",
        {"database": "shop.db", "sql": "SELECT name FROM items WHERE price > ?", "params": [1]},
    )
    assert r.structured_content["rows"] == [["apple"]]
    cols = await engine.call_tool(
        "sqlite_describe_table", {"database": "shop.db", "table": "items"}
    )
    assert '"primary_key": true' in text_of(cols).replace("True", "true").lower()


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM items",
        "INSERT INTO items(name) VALUES('x')",
        "DROP TABLE items",
        "SELECT 1; DELETE FROM items",
    ],
)
async def test_sqlite_rejects_writes(engine, sample_db, sql):
    r = await engine.call_tool("sqlite_query", {"database": "shop.db", "sql": sql})
    assert r.is_error
    with sqlite3.connect(sample_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 2
