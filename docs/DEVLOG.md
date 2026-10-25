# MCP Nexus — Development Log

A running record of what was built, why, and how the pieces connect. Newest entries at the bottom.

---

## Phase 1 — Scaffold & architecture · 2026-10-25

### Built
- Repository layout: `nexus/` (Python package), `nexus/connectors/`, `nexus/api/`, `web/templates`, `web/static`, `plugins/`, `tests/`, `docs/`, plus git-ignored `data/` and `workspace/` (kept with `.gitkeep`).
- `pyproject.toml` (hatchling, Python ≥ 3.12, `dev` extra with pytest/ruff) and a mirrored `requirements.txt`.
- `.gitignore` (secrets, SQLite files, venvs, caches, OS cruft) and a fully commented `.env.example`.
- `nexus/config.py`: a `pydantic-settings` `Settings` class. `NEXUS_`-prefixed variables, plus standard provider names (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OLLAMA_BASE_URL`). Empty values in `.env` are ignored so blank keys fall back to defaults.
- `tests/test_config.py`: safe-defaults and env-override tests (2 passing).
- `docs/ARCHITECTURE.md`: system diagram, module map, component designs, data model, security model and ADR table.
- MIT `LICENSE`, placeholder `README.md` (full version in Phase 5).

### Decisions
- **mcp 2.x, not 1.x.** Checking the SDK showed the current release is `mcp 2.3.0`, where `FastMCP` was renamed to `MCPServer` (`mcp.server.mcpserver`). It still has `add_tool` / `remove_tool` / `add_prompt`, which is everything the dynamic registry needs, so we target 2.x (`mcp>=2.3,<3`) instead of pinning the old API.
- **Host as an MCP client of itself** (ADR 1). The chat agent never calls connector Python directly; it goes through an in-memory MCP client session. That gives one code path for the web UI and external clients, and lets the Inspector tap genuine JSON-RPC traffic.
- **Approval gate lives in the server** (ADR 7), so outbound tools are protected even when Claude Desktop or another external client calls them.
- **SSE instead of WebSockets** (ADR 4): every real-time stream is server → browser.
- **Localhost-only by default**; plugins are documented as trusted code.

### How it fits together (so far)
Only configuration is executable at this point. `Settings` is the single source for paths (`data_dir`, `workspace_dir`, `plugins_dir`) and limits that every later module reads via `get_settings()`.

### Next (Phase 2)
`db.py` + migrations, `secrets.py`, `events.py` bus, `mcp_engine.py` (server, registry, RPC tap, in-memory client), the `Connector` base class, and the Filesystem, Outbound (webhook/email) and Web Fetcher connectors with tests.
