"""Read-only SQL against SQLite files in the workspace (or an explicit allow-list).

Databases are opened with ``mode=ro`` plus ``PRAGMA query_only``, only one
statement is accepted per call, and results are capped.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

import aiosqlite
from pydantic import BaseModel, Field

from nexus.connectors.base import Connector, ToolError, tool

DB_SUFFIXES = {".db", ".sqlite", ".sqlite3"}


class SQLiteQueryConfig(BaseModel):
    extra_databases: list[str] = Field(
        default_factory=list,
        description="Absolute paths of extra SQLite files to allow (read-only).",
    )
    max_rows: int = Field(500, ge=1, le=10_000)


class SQLiteQueryConnector(Connector):
    name = "sqlite"
    description = "Run read-only SQL queries against SQLite databases in the workspace."
    Config = SQLiteQueryConfig

    config: SQLiteQueryConfig

    def _databases(self) -> dict[str, Path]:
        root = self.ctx.settings.workspace_dir.resolve()
        found: dict[str, Path] = {}
        if root.is_dir():
            for p in sorted(root.rglob("*")):
                if p.suffix.lower() in DB_SUFFIXES and p.is_file():
                    found[p.relative_to(root).as_posix()] = p
        for extra in self.config.extra_databases:
            p = Path(extra).expanduser().resolve()
            if p.is_file():
                found[str(p)] = p
        return found

    def _path(self, database: str) -> Path:
        dbs = self._databases()
        if database not in dbs:
            raise ToolError(
                f"Unknown database {database!r}. Available: {', '.join(dbs) or 'none'}."
            )
        return dbs[database]

    async def _run(
        self, database: str, sql: str, params: list[Any] | None = None
    ) -> dict[str, Any]:
        path = self._path(database)
        uri = f"{path.as_uri()}?mode=ro"
        try:
            async with aiosqlite.connect(uri, uri=True) as conn:
                await conn.execute("PRAGMA query_only=ON")
                async with conn.execute(sql, params or []) as cur:
                    rows = await cur.fetchmany(self.config.max_rows + 1)
                    columns = [d[0] for d in cur.description or []]
        except (sqlite3.Error, ValueError) as exc:
            raise ToolError(f"SQLite error: {exc}") from None
        truncated = len(rows) > self.config.max_rows
        return {
            "columns": columns,
            "rows": [list(r) for r in rows[: self.config.max_rows]],
            "row_count": min(len(rows), self.config.max_rows),
            "truncated": truncated,
        }

    @tool(read_only=True)
    async def list_databases(self) -> list[str]:
        """SQLite files you can query (relative to the workspace, or absolute for extras)."""
        return list(self._databases())

    @tool(read_only=True)
    async def list_tables(self, database: str) -> list[str]:
        """Tables and views in a database."""
        res = await self._run(
            database,
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name",
        )
        return [r[0] for r in res["rows"]]

    @tool(read_only=True)
    async def describe_table(self, database: str, table: str) -> list[dict]:
        """Column names, types, nullability and primary-key flags for a table."""
        if table not in await self.list_tables(database):
            raise ToolError(f"No table {table!r} in {database!r}.")
        res = await self._run(
            database, 'SELECT name, type, "notnull", pk FROM pragma_table_info(?)', [table]
        )
        return [
            {"name": n, "type": t, "not_null": bool(nn), "primary_key": bool(pk)}
            for n, t, nn, pk in res["rows"]
        ]

    @tool(read_only=True)
    async def query(
        self,
        database: str,
        sql: Annotated[str, Field(description="A single read-only SQL statement")],
        params: Annotated[list[Any] | None, Field(description="Positional ? parameters")] = None,
    ) -> dict[str, Any]:
        """Run one read-only SQL statement and return columns + rows."""
        if not sqlite3.complete_statement(sql.rstrip().rstrip(";") + ";"):
            raise ToolError("Incomplete SQL statement.")
        return await self._run(database, sql, params)
