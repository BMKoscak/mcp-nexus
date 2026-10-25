"""SQLite persistence (aiosqlite, no ORM).

Schema versions are tracked with ``PRAGMA user_version``; each entry in
``MIGRATIONS`` upgrades the database by exactly one version. Repositories are
plain methods on :class:`Database` so callers never write SQL themselves.
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE providers (
        name        TEXT PRIMARY KEY,
        base_url    TEXT,
        api_key_enc TEXT,
        enabled     INTEGER NOT NULL DEFAULT 1,
        extra       TEXT NOT NULL DEFAULT '{}',
        updated_at  REAL NOT NULL
    );
    CREATE TABLE connector_state (
        name       TEXT PRIMARY KEY,
        enabled    INTEGER NOT NULL DEFAULT 1,
        config     TEXT NOT NULL DEFAULT '{}',
        updated_at REAL NOT NULL
    );
    CREATE TABLE webhook_targets (
        name        TEXT PRIMARY KEY,
        kind        TEXT NOT NULL DEFAULT 'http',
        url_enc     TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        created_at  REAL NOT NULL
    );
    CREATE TABLE templates (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '',
        body        TEXT NOT NULL,
        arguments   TEXT NOT NULL DEFAULT '[]',
        created_at  REAL NOT NULL,
        updated_at  REAL NOT NULL
    );
    CREATE TABLE conversations (
        id         TEXT PRIMARY KEY,
        title      TEXT NOT NULL DEFAULT 'New chat',
        model      TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE messages (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        role            TEXT NOT NULL,
        content         TEXT,
        tool_calls      TEXT,
        tool_call_id    TEXT,
        name            TEXT,
        created_at      REAL NOT NULL
    );
    CREATE INDEX idx_messages_conv ON messages(conversation_id, id);
    CREATE TABLE rpc_log (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         REAL NOT NULL,
        transport  TEXT NOT NULL,
        direction  TEXT NOT NULL,
        kind       TEXT NOT NULL,
        method     TEXT,
        rpc_id     TEXT,
        latency_ms REAL,
        payload    TEXT NOT NULL
    );
    CREATE TABLE pending_actions (
        id         TEXT PRIMARY KEY,
        tool       TEXT NOT NULL,
        connector  TEXT NOT NULL,
        summary    TEXT NOT NULL,
        arguments  TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'pending',
        created_at REAL NOT NULL,
        decided_at REAL,
        result     TEXT
    );
    """,
]


def _now() -> float:
    return time.time()


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._conn: aiosqlite.Connection | None = None

    # ── lifecycle ────────────────────────────────────────────
    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self.migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def schema_version(self) -> int:
        async with self.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        return int(row[0])

    async def migrate(self) -> None:
        version = await self.schema_version()
        for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
            await self.conn.executescript(
                f"BEGIN;\n{script}\nPRAGMA user_version={target};\nCOMMIT;"
            )

    # ── generic helpers ──────────────────────────────────────
    async def _all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        async with self.conn.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def _one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        async with self.conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def _exec(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur.lastrowid or 0

    # ── settings ─────────────────────────────────────────────
    async def get_setting(self, key: str, default: Any = None) -> Any:
        row = await self._one("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self._exec(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    # ── providers ────────────────────────────────────────────
    async def get_provider(self, name: str) -> dict[str, Any] | None:
        row = await self._one("SELECT * FROM providers WHERE name=?", (name,))
        if row:
            row["extra"] = json.loads(row["extra"])
            row["enabled"] = bool(row["enabled"])
        return row

    async def list_providers(self) -> list[dict[str, Any]]:
        rows = await self._all("SELECT * FROM providers ORDER BY name")
        for r in rows:
            r["extra"] = json.loads(r["extra"])
            r["enabled"] = bool(r["enabled"])
        return rows

    async def upsert_provider(
        self,
        name: str,
        *,
        base_url: str | None = None,
        api_key_enc: str | None = None,
        enabled: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> None:
        await self._exec(
            "INSERT INTO providers(name, base_url, api_key_enc, enabled, extra, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
            "base_url=excluded.base_url, api_key_enc=COALESCE(excluded.api_key_enc, api_key_enc), "
            "enabled=excluded.enabled, extra=excluded.extra, updated_at=excluded.updated_at",
            (name, base_url, api_key_enc, int(enabled), json.dumps(extra or {}), _now()),
        )

    async def clear_provider_key(self, name: str) -> None:
        await self._exec("UPDATE providers SET api_key_enc=NULL WHERE name=?", (name,))

    # ── connector state ──────────────────────────────────────
    async def get_connector_state(self, name: str) -> dict[str, Any] | None:
        row = await self._one("SELECT * FROM connector_state WHERE name=?", (name,))
        if row:
            row["enabled"] = bool(row["enabled"])
            row["config"] = json.loads(row["config"])
        return row

    async def set_connector_state(
        self, name: str, *, enabled: bool | None = None, config: dict[str, Any] | None = None
    ) -> None:
        current = await self.get_connector_state(name) or {"enabled": True, "config": {}}
        await self._exec(
            "INSERT INTO connector_state(name, enabled, config, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled, config=excluded.config, "
            "updated_at=excluded.updated_at",
            (
                name,
                int(current["enabled"] if enabled is None else enabled),
                json.dumps(current["config"] if config is None else config),
                _now(),
            ),
        )

    # ── webhook targets ──────────────────────────────────────
    async def list_webhook_targets(self) -> list[dict[str, Any]]:
        return await self._all("SELECT * FROM webhook_targets ORDER BY name")

    async def get_webhook_target(self, name: str) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM webhook_targets WHERE name=?", (name,))

    async def upsert_webhook_target(
        self, name: str, *, kind: str, url_enc: str, description: str = ""
    ) -> None:
        await self._exec(
            "INSERT INTO webhook_targets(name, kind, url_enc, description, created_at) "
            "VALUES(?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET kind=excluded.kind, "
            "url_enc=excluded.url_enc, description=excluded.description",
            (name, kind, url_enc, description, _now()),
        )

    async def delete_webhook_target(self, name: str) -> None:
        await self._exec("DELETE FROM webhook_targets WHERE name=?", (name,))

    # ── templates ────────────────────────────────────────────
    async def list_templates(self) -> list[dict[str, Any]]:
        rows = await self._all("SELECT * FROM templates ORDER BY name")
        for r in rows:
            r["arguments"] = json.loads(r["arguments"])
        return rows

    async def get_template(self, name: str) -> dict[str, Any] | None:
        row = await self._one("SELECT * FROM templates WHERE name=?", (name,))
        if row:
            row["arguments"] = json.loads(row["arguments"])
        return row

    async def upsert_template(
        self, name: str, *, body: str, description: str = "", arguments: list[dict] | None = None
    ) -> None:
        now = _now()
        await self._exec(
            "INSERT INTO templates(name, description, body, arguments, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
            "description=excluded.description, body=excluded.body, "
            "arguments=excluded.arguments, updated_at=excluded.updated_at",
            (name, description, body, json.dumps(arguments or []), now, now),
        )

    async def delete_template(self, name: str) -> None:
        await self._exec("DELETE FROM templates WHERE name=?", (name,))

    # ── conversations ────────────────────────────────────────
    async def create_conversation(self, title: str = "New chat", model: str | None = None) -> str:
        cid = uuid.uuid4().hex
        now = _now()
        await self._exec(
            "INSERT INTO conversations(id, title, model, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?)",
            (cid, title, model, now, now),
        )
        return cid

    async def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._all(
            "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?", (limit,)
        )

    async def get_conversation(self, cid: str) -> dict[str, Any] | None:
        return await self._one("SELECT * FROM conversations WHERE id=?", (cid,))

    async def update_conversation(
        self, cid: str, *, title: str | None = None, model: str | None = None
    ) -> None:
        await self._exec(
            "UPDATE conversations SET title=COALESCE(?, title), model=COALESCE(?, model), "
            "updated_at=? WHERE id=?",
            (title, model, _now(), cid),
        )

    async def delete_conversation(self, cid: str) -> None:
        await self._exec("DELETE FROM conversations WHERE id=?", (cid,))

    async def add_message(
        self,
        cid: str,
        role: str,
        content: str | None = None,
        *,
        tool_calls: list[dict] | None = None,
        tool_call_id: str | None = None,
        name: str | None = None,
    ) -> int:
        mid = await self._exec(
            "INSERT INTO messages(conversation_id, role, content, tool_calls, tool_call_id, "
            "name, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                role,
                content,
                json.dumps(tool_calls) if tool_calls else None,
                tool_call_id,
                name,
                _now(),
            ),
        )
        await self._exec("UPDATE conversations SET updated_at=? WHERE id=?", (_now(), cid))
        return mid

    async def list_messages(self, cid: str) -> list[dict[str, Any]]:
        rows = await self._all("SELECT * FROM messages WHERE conversation_id=? ORDER BY id", (cid,))
        for r in rows:
            r["tool_calls"] = json.loads(r["tool_calls"]) if r["tool_calls"] else None
        return rows

    # ── rpc log ──────────────────────────────────────────────
    async def add_rpc_entries(self, entries: list[dict[str, Any]], keep: int) -> None:
        if not entries:
            return
        await self.conn.executemany(
            "INSERT INTO rpc_log(ts, transport, direction, kind, method, rpc_id, latency_ms, "
            "payload) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    e["ts"],
                    e["transport"],
                    e["direction"],
                    e["kind"],
                    e.get("method"),
                    None if e.get("rpc_id") is None else str(e["rpc_id"]),
                    e.get("latency_ms"),
                    json.dumps(e["payload"]),
                )
                for e in entries
            ],
        )
        await self.conn.execute(
            "DELETE FROM rpc_log WHERE id <= (SELECT MAX(id) FROM rpc_log) - ?", (keep,)
        )
        await self.conn.commit()

    async def list_rpc_entries(self, limit: int = 500) -> list[dict[str, Any]]:
        rows = await self._all(
            "SELECT * FROM (SELECT * FROM rpc_log ORDER BY id DESC LIMIT ?) ORDER BY id", (limit,)
        )
        for r in rows:
            r["payload"] = json.loads(r["payload"])
        return rows

    async def clear_rpc_log(self) -> None:
        await self._exec("DELETE FROM rpc_log")

    # ── pending actions (approval audit) ─────────────────────
    async def add_pending_action(
        self, aid: str, *, tool: str, connector: str, summary: str, arguments: dict[str, Any]
    ) -> None:
        await self._exec(
            "INSERT INTO pending_actions(id, tool, connector, summary, arguments, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (aid, tool, connector, summary, json.dumps(arguments, default=str), _now()),
        )

    async def decide_pending_action(self, aid: str, status: str, result: str | None = None) -> None:
        await self._exec(
            "UPDATE pending_actions SET status=?, decided_at=?, result=? WHERE id=?",
            (status, _now(), result, aid),
        )

    async def list_pending_actions(
        self, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        if status:
            rows = await self._all(
                "SELECT * FROM pending_actions WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = await self._all(
                "SELECT * FROM pending_actions ORDER BY created_at DESC LIMIT ?", (limit,)
            )
        for r in rows:
            r["arguments"] = json.loads(r["arguments"])
        return rows
