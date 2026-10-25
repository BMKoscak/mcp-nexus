# MCP Nexus — Architecture

> Status: **Phase 4 implemented** (engine, persistence, connectors, FastAPI backend, LLM router, agent, templates, dashboard). Sections marked *(planned)* describe modules that later phases implement; this document is updated as they land.

## 1. Goals

| Goal | What it means in practice |
|---|---|
| **Local-first** | Runs on `127.0.0.1`, single process, SQLite on disk. Works fully offline with Ollama. |
| **Protocol-honest** | The host talks to tools *only* through real MCP JSON-RPC 2.0. This keeps the Inspector truthful and lets external MCP clients (Claude Desktop, Cursor, etc.) use the same tools. |
| **Pluggable** | A connector is one Python class. Drop it in `plugins/` and its tools appear without touching core code. |
| **Human in the loop** | Anything that sends data out of the machine (webhooks, email, notifications) can require approval in the UI. |
| **Zero-build frontend** | Jinja2 + Tailwind (CDN) + HTMX + Alpine.js. No Node toolchain. |

## 2. System overview

```mermaid
flowchart LR
    subgraph Browser["Browser — Tailwind · HTMX · Alpine.js"]
        UI_Chat["Chat & Execution"]
        UI_Conn["Connections / LLM Hub"]
        UI_Tpl["Template Studio"]
        UI_Insp["Live Inspector"]
    end

    subgraph Host["FastAPI Host (nexus.main)"]
        API["REST + HTMX routes"]
        SSE["SSE streams<br/>(chat tokens, rpc log, approvals)"]
        Router["LLM Router<br/>(httpx provider adapters)"]
        Agent["Agent loop<br/>(tool-call orchestration)"]
        Client["MCP Client<br/>(in-memory transport)"]
        Tap["RPC Tap → Event Bus"]
        Approve["Approval Gate"]
        TplMgr["Template Manager"]
        DB[("SQLite<br/>aiosqlite")]
    end

    subgraph Engine["MCP Engine (nexus.mcp_engine)"]
        Server["MCPServer (mcp 2.x)"]
        Registry["Connector Registry"]
        subgraph Connectors
            FS["Filesystem"]
            WH["Webhook / Email<br/>(outbound)"]
            WF["Web Fetcher"]
            SYS["System Stats"]
            SQLQ["SQLite Query"]
            PLUG["plugins/*.py"]
        end
    end

    LLMs["Anthropic · OpenAI · Ollama"]
    Ext["External MCP clients<br/>(Claude Desktop, Cursor…)"]

    UI_Chat -- "POST /api/chat" --> API
    UI_Conn --> API
    UI_Tpl --> API
    SSE -- "EventSource" --> UI_Chat
    SSE --> UI_Insp
    API --> Agent
    Agent <--> Router
    Router <--> LLMs
    Agent <--> Client
    Client <-- "JSON-RPC 2.0" --> Tap
    Tap <-- "JSON-RPC 2.0" --> Server
    Tap --> SSE
    Server --> Registry --> Connectors
    WH -.-> Approve -.-> SSE
    TplMgr --> Server
    API --> DB
    TplMgr --> DB
    Tap --> DB
    Ext -- "Streamable HTTP /mcp<br/>or stdio" --> Server
```

## 3. Module map

```
mcp-nexus/
├── nexus/
│   ├── config.py           Settings (env + .env), paths              [Phase 1 ✔]
│   ├── db.py               aiosqlite schema, migrations, repositories [Phase 2 ✔]
│   ├── crypto.py           Fernet encryption + masking for secrets     [Phase 2 ✔]
│   ├── events.py           In-process async pub/sub event bus          [Phase 2 ✔]
│   ├── approvals.py        Pending-action gate for outbound tools      [Phase 2 ✔]
│   ├── netguard.py         SSRF guard (blocks private/loopback IPs)    [Phase 2 ✔]
│   ├── mcp_engine.py       MCPServer + RPC tap + tapped transport      [Phase 2 ✔]
│   ├── registry.py         Connector discovery, lifecycle, config      [Phase 2 ✔]
│   ├── connectors/
│   │   ├── base.py         Connector base class + @tool/@resource      [Phase 2 ✔]
│   │   ├── filesystem.py   Sandboxed read/write/list/search            [Phase 2 ✔]
│   │   ├── outbound.py     Webhooks, Discord, Slack, email, ntfy push  [Phase 2 ✔]
│   │   ├── web_fetch.py    Fetch → markdown/text, links, JSON          [Phase 2 ✔]
│   │   ├── system.py       CPU/RAM/disk/process stats (psutil)         [Phase 2 ✔]
│   │   └── sqlite_query.py Read-only SQL against workspace DBs         [Phase 2 ✔]
│   ├── llm_router.py       Provider credentials, models, tests, routing [Phase 3 ✔]
│   ├── providers/          base · anthropic · openai · ollama (httpx)  [Phase 3 ✔]
│   ├── agent.py            LLM ⇄ MCP tool-calling loop                 [Phase 3 ✔]
│   ├── templates.py        Jinja2-sandboxed templates → MCP prompts    [Phase 3 ✔]
│   ├── api/                system · llm · connectors · approvals ·     [Phase 3 ✔]
│   │                       templates · chat routers (+ deps helpers)
│   ├── main.py             App factory, lifespan, /mcp routes          [Phase 3 ✔]
│   └── cli.py              nexus serve · stdio · tools                 [Phase 3 ✔]
├── web/templates/        index.html + partials/ (HTMX fragments)      [Phase 4 ✔]
├── web/static/           app.css (tokens, components) · app.js (Alpine) [Phase 4 ✔]
├── plugins/                User connectors, auto-discovered
├── tests/                  pytest (+ pytest-asyncio)
├── data/                   SQLite DB, secret.key (git-ignored)
└── workspace/              Filesystem connector sandbox (git-ignored)
```

## 4. Key components

### 4.1 MCP Engine *(Phase 2 ✔ — `nexus/mcp_engine.py`)*

- Built on **`mcp` 2.3**. In 2.x the former `FastMCP` class is `mcp.server.mcpserver.MCPServer`; it exposes `add_tool` / `remove_tool` / `add_prompt` / `add_resource`, which is what makes runtime (un)registration possible. The SDK negotiates the **`2026-07-28`** protocol revision (`server/discover` instead of the old `initialize` handshake) and falls back to legacy servers automatically.
- **Why a custom transport:** handed an in-process `MCPServer`, the 2.x `Client` uses a *direct dispatcher* with no JSON-RPC framing at all — there would be nothing real for the Inspector to show. `TappedTransport` instead runs the low-level server over two memory-stream pairs joined by relay tasks:

  ```
  host Client ⇄ client streams ⇄ relay (c2s/s2c, tapped) ⇄ server streams ⇄ MCPServer
  ```

  Each relay copies the `SessionMessage` to `RpcTap.record()` before forwarding it, so the log is the exact JSON-RPC the server sent and received.
- **Supervisor task:** the client session owns anyio task groups, which must be entered and exited in the same task. The engine runs the session in a dedicated `mcp-host-client` task and signals it to stop, so `start()`/`stop()` can be called from different tasks (FastAPI lifespan, pytest fixtures).
- The host client is created with `cache=None` because tools change at runtime and the SDK's response cache would otherwise serve stale `tools/list` results.
- **One server, three transports:**
  1. **In-memory** — the FastAPI host connects its own `Client` to the server over memory streams. This is the path the web UI uses.
  2. **Streamable HTTP** — `server.streamable_http_app()` is mounted at `/mcp` so external clients can connect to `http://127.0.0.1:8765/mcp`.
  3. **stdio** — `nexus stdio` runs the same server for Claude Desktop-style configs.
- **RPC Tap:** each message becomes an entry `{seq, ts, transport, session, direction: c2s|s2c, kind: request|response|notification|error, method, rpc_id, latency_ms, payload}`. Responses are paired with their request by `(session, direction, id)` to fill in `method` and `latency_ms`. Entries are published on the `rpc` bus topic immediately and written to `rpc_log` in 0.5 s batches (ring buffer, default 2,000 rows). - **External clients** (Streamable HTTP on `/mcp`, or `nexus stdio`) never pass through `TappedTransport`, so they are recorded by `ExternalTap`, an MCP **server middleware** (`ServerMiddleware`, new in 2.x) that sees the method, id, raw params and result of every inbound request on any transport. It rebuilds the JSON-RPC envelope in the same entry format (`transport: http|stdio`, `session` = the client's `clientInfo.name`). It skips host traffic (no HTTP request and not in stdio mode) so nothing is logged twice.
- **`/mcp` mounting:** `streamable_http_app(streamable_http_path="/mcp")` is built at app creation and its routes are spliced into the FastAPI router, rather than `app.mount("/mcp", …)`, which would 307-redirect `/mcp` → `/mcp/`. The app lifespan runs `session_manager.run()`. The SDK's DNS-rebinding protection is kept (`host=settings.host`); `localhost` and `127.0.0.1` both work.

### 4.2 Connector Registry *(Phase 2 ✔ — `nexus/registry.py`, `nexus/connectors/base.py`)*

A connector is a class; methods decorated with `@tool` become MCP tools and `@resource("uri")` become MCP resources. Type hints and the docstring/description produce the JSON Schema automatically (via the SDK's function introspection).

```python
from nexus.connectors import Connector, tool

class Dice(Connector):
    name = "dice"
    description = "Roll dice"

    @tool()
    async def roll(self, sides: int = 6) -> int:
        """Roll one die."""
        import random
        return random.randint(1, sides)
```

- **Discovery:** built-ins in `nexus/connectors/`, user files in `plugins/`, and the `mcp_nexus.connectors` entry-point group for pip-installed packages.
- **Naming:** tools are published as `<connector>_<tool>` → e.g. `filesystem_read_file`. Dots were ruled out because Anthropic and OpenAI both require tool names to match `^[A-Za-z0-9_-]{1,64}$`; the registry validates every name against that pattern.
- **Lifecycle:** enable/disable per connector is stored in `connector_state`; toggling calls `add_tool`/`remove_tool` and emits `notifications/tools/list_changed` so connected clients refresh.
- **Config:** a connector may declare a Pydantic `Config` model; `config_view()` returns its JSON Schema + current values for a UI form, and `update_config()` validates, persists and hot-restarts the connector. Fields typed `SecretStr` are stored Fernet-encrypted (`{"__enc__": token}`) and shown masked; posting the mask back leaves the secret unchanged.
- **Approval wrapping:** for `@tool(requires_approval=True)` the registry registers a `functools.wraps` wrapper that calls the approval gate first. `wraps` preserves the signature and annotations, so the generated JSON Schema is identical to the unwrapped method's.
- **Tool metadata:** every tool carries `_meta` `{"nexus/connector", "nexus/requiresApproval"}` and, where declared, MCP annotations (`readOnlyHint`, `destructiveHint`, `openWorldHint`) so the UI can badge tools.
- **Errors:** `ToolError("…")` messages reach the model verbatim; any other exception is masked by the SDK as a generic "Error executing tool" so internals don't leak.
- **Isolation of failures:** a plugin that fails to import is shown as *errored* in the UI with its traceback; it never prevents startup.

### 4.3 Outbound Dispatcher & Approval Gate *(Phase 2 ✔ — `nexus/approvals.py`, `nexus/connectors/outbound.py`)*

Outbound tools (`outbound_send_webhook`, `outbound_send_discord`, `outbound_send_slack`, `outbound_send_email`, `outbound_notify` via ntfy) are marked `requires_approval=True`; `outbound_list_targets` is read-only.

```mermaid
sequenceDiagram
    participant LLM
    participant Agent
    participant MCP as MCP Server
    participant Gate as Approval Gate
    participant UI
    LLM->>Agent: tool_call outbound.send_discord
    Agent->>MCP: tools/call
    MCP->>Gate: request(action)
    alt auto-approve ON
        Gate-->>MCP: approved
    else auto-approve OFF
        Gate-->>UI: SSE approvals "requested" (+ pending row in SQLite)
        UI->>Gate: POST /api/approvals/{id} (approve / deny)
        Gate-->>MCP: decision (or timeout → denied)
    end
    MCP-->>Agent: result / isError
    Agent-->>LLM: tool_result
```

- **Decisions travel through SQLite** so they work across processes: Claude Desktop runs `nexus stdio` as its own process with no UI, while the user's browser talks to `nexus serve`. The waiting call polls its `pending_actions` row every 0.5 s; `decide()` sets the row with a conditional `UPDATE … WHERE status='pending'` (first writer wins) and, when the waiter lives in the same process, also resolves its in-memory future for an instant wake-up. Rows end as `approved`, `denied`, `timeout` or `auto`, which doubles as the audit trail. The auto-approve toggle is stored in `settings` so it survives restarts; `NEXUS_AUTO_APPROVE_OUTBOUND` is only the initial default.
- Approval cards show a human summary rendered from the tool's `summary=` template (e.g. `Discord (team): hello`) plus the arguments. Editing arguments before approving was considered and deferred: edited values would bypass the SDK's argument validation.
- Named targets (`discord`, `slack`, saved custom webhooks) resolve server-side so the LLM never sees webhook URLs.
- External MCP clients hit the same gate: their calls wait for approval in the Nexus UI (or time out).

### 4.4 LLM Router *(Phase 3 ✔ — `nexus/llm_router.py`, `nexus/providers/`)*

- **Direct async clients over `httpx`** — one small adapter per provider behind a common `Provider` protocol:
  - `AnthropicProvider` → `POST /v1/messages` (SSE stream, `tool_use` / `tool_result` blocks)
  - `OpenAIProvider` → `POST /v1/chat/completions` (SSE stream, `tool_calls`); also works for any OpenAI-compatible endpoint (LM Studio, vLLM, OpenRouter) via a custom base URL
  - `OllamaProvider` → `POST /api/chat` (NDJSON stream, native `tools`)
- Each adapter normalises into one internal shape: `TextDelta`, `ToolCall(id, name, arguments)`, `Done(usage, stop_reason)`. The agent loop only sees that shape.
- Models are addressed as `<provider>/<model>` (e.g. `anthropic/claude-sonnet-5-5`, `ollama/llama3.2`).
- Provider credentials resolve in order: environment → encrypted SQLite (`providers` table) → none.
- `GET /api/llm/models` queries every provider's live model list in parallel (`/v1/models`, `/models`, `/api/tags`) with an 8 s cap, reporting per-provider errors (`no API key`, `connection refused`) instead of failing.
- "Test connection" lists models and, given a model, streams a tiny "pong" completion; it reports latency or the provider's own error message.
- Anthropic specifics: tool results are sent as user-role `tool_result` blocks, consecutive same-role turns are merged (strict alternation), `max_tokens` defaults to 4096. OpenAI: `max_completion_tokens`, `stream_options.include_usage`, tool-call fragments reassembled by `index`; no `Authorization` header is sent to non-OpenAI base URLs without a key. Ollama: NDJSON lines, tool calls get generated ids, tool results carry `tool_name`.
- The active model is a persisted setting (`NEXUS_DEFAULT_MODEL` is the initial default).

### 4.5 Agent loop *(Phase 3 ✔ — `nexus/agent.py`)*

1. `engine.list_tools()` (a real `tools/list`) → neutral `ToolDef`s; each adapter converts them to its vendor schema.
2. Stream the completion; text deltas go straight to the browser as SSE `text` events.
3. Tool calls run sequentially via `engine.call_tool()` (through the tap, so each one shows in the Inspector; gated tools pause here for approval). Results are truncated to 20k characters, appended to the history, and the loop repeats, capped by `max_steps` (default 8).
4. Every user, assistant and tool message is persisted, so a conversation can be resumed; dangling tool calls from an interrupted stream are dropped when the history is reloaded (providers reject a `tool_use` without a result).
5. If a model rejects tools ("does not support tools", common with small Ollama models), the agent emits a `notice` and retries without tools instead of failing.

Event stream: `conversation` → (`text` | `tool_call` | `tool_result` | `notice`)* → `done` | `error`.

### 4.6 Template Studio *(Phase 3 ✔ — `nexus/templates.py`)*

- Templates are stored in SQLite with a name, description, body and an argument list (name, description, required, default). If arguments are omitted they are inferred from the body with `jinja2.meta.find_undeclared_variables`; declared lists are topped up with any variables they miss.
- Bodies use full Jinja2 syntax (`{{ var }}`, `{{ x | default('…') }}`, `{% if %}`) rendered in a **`SandboxedEnvironment`** with **`StrictUndefined`**: no access to Python internals (`''.__class__.__mro__` raises), and a missing variable is an error rather than silent blank text. Optional arguments with no value stay undefined so `| default()` applies.
- Each template is registered as an **MCP Prompt**, so `prompts/list` and `prompts/get` work for external clients too. Preview renders via `prompts/get` (visible in the Inspector); "Run" renders and streams the agent's answer with the same events as chat.
- Three starter templates are seeded once: `summarize-url`, `system-health-report`, `save-research-notes`.

### 4.7 Live Inspector *(Phase 4 ✔ — `web/static/app.js` → `inspector`)*

- Event bus topic `rpc` → `GET /api/events?topics=rpc` (SSE; the same endpoint multiplexes `approvals` and `connectors`). History: `GET /api/inspector/history`.
- A **two-lane wire**: requests sit left of a central spine with a teal tick, replies sit right with a violet tick, notifications are dashed and errors red. Rows are grouped by source (this dashboard, HTTP client, stdio client + client name).
- Selecting a message shows its exact JSON (syntax-coloured with the same teal/violet), source, time, request id and round-trip; "Show the reply / Show the request" jumps to its pair (matched by `session` + `id`).
- Filter by method or payload text and by source; pause buffers incoming messages and shows a count; export the visible set as JSON; clear empties `rpc_log`.
- The last 500 rows load from `GET /api/inspector/history` on open, then live entries append from the shared event stream (capped at 2,000 in memory).

### 4.8 Dashboard *(Phase 4 ✔ — `web/`)*

| Tab | What it does | Data |
|---|---|---|
| **Chat** | Conversation list (HTMX fragment), streaming replies rendered as ordered parts (text → tool → text), collapsible tool rows (teal = sent, violet = returned, amber = waiting), inline Approve/Deny cards, model picker, tools on/off, stop, delete. Approvals raised by *other* clients (Claude Desktop via stdio) also appear here. | `POST /api/chat` (SSE on the response), `/api/conversations`, `/ui/conversations` |
| **Connect** | Provider cards (masked key + source, server address, live model count, test with a model), "Send without asking" switch, named destinations (host-only masking), connector cards with on/off switches and tool lists (HTMX), settings modal generated from each connector's JSON Schema. | `/api/llm/*`, `/api/approvals/auto`, `/api/targets`, `/ui/connectors/*`, `/api/connectors/{name}/config` |
| **Templates** | List, editor with live blank detection (debounced `validate`), per-blank hint/fallback/required, Preview (via MCP `prompts/get`), Run in chat. | `/api/templates/*` |
| **Inspector** | See 4.7. | `/api/events?topics=rpc`, `/api/inspector/history` |

- **One `EventSource`** (`/api/events?topics=rpc,approvals,connectors`) feeds the whole page; the shell re-dispatches `rpc` entries as `nexus:rpc` window events. Approvals are additionally polled every 4 s because a stdio process can't publish on this process's bus.
- **HTMX** renders the parts that are naturally server-side lists (connector cards, conversation list) and swaps a single card after a toggle. **Alpine** owns everything stateful (streams, forms, the inspector).
- **Visual system:** colour tokens on `:root` with automatic dark mode (`prefers-color-scheme`) and a manual Auto/Light/Dark switch (`data-theme`). Two signal colours carry meaning everywhere: teal for client→server, violet for server→client; amber only for approval. Instrument Sans for UI, Spline Sans Mono only for code/JSON. Responsive down to phone width (rail becomes a bottom bar; the conversation list becomes an overlay), visible focus rings, `prefers-reduced-motion` respected.
- Per-viewer conveniences (current tab, theme, open conversation) live in `localStorage`, wrapped in try/catch; the page works without it.

## 5. Data model (SQLite) *(Phase 2 ✔ — `nexus/db.py`)*

| Table | Purpose |
|---|---|
| `settings` | Key/value runtime settings (e.g. `auto_approve_outbound`, `active_model`) |
| `providers` | Provider name, base URL, **encrypted** API key, enabled flag |
| `connector_state` | Connector name, enabled flag, JSON config |
| `webhook_targets` | Named custom webhook destinations (URL encrypted) |
| `templates` | Prompt templates + JSON argument schema |
| `conversations` / `messages` | Chat history incl. tool calls/results |
| `rpc_log` | Ring buffer of tapped JSON-RPC messages (v2 migration added `session`) |
| `pending_actions` | Approval audit trail (payload, decision, decided_at) |

Schema versioning uses `PRAGMA user_version` with ordered migration functions; WAL mode is enabled for concurrent reads during streaming.

## 6. Security model

- **Bind to localhost** by default. No auth layer in v1, so exposing it on a network is explicitly unsupported.
- **Secrets:** UI-entered keys are encrypted with Fernet (`NEXUS_SECRET_KEY` or an auto-generated `data/secret.key`), never returned to the browser in full (masked `sk-…abcd`).
- **Filesystem sandbox:** all paths resolved with `Path.resolve()` and required to be inside `NEXUS_WORKSPACE_DIR`; symlink escapes rejected; size limits on read/write.
- **Web fetcher SSRF guard:** resolves the hostname and refuses loopback/private/link-local/reserved ranges (incl. IPv4-mapped IPv6) unless `NEXUS_FETCH_BLOCK_PRIVATE=false`; redirects are followed manually and every hop is re-checked; response size and time capped. Known limitation: a DNS-rebinding attacker could change the answer between the check and httpx's own lookup.
- **Outbound webhooks are *not* SSRF-filtered** — posting to a local n8n/Home Assistant is a legitimate use — but they are approval-gated, and raw URLs can be disabled in favour of named targets.
- **SQLite query tool:** only files inside the workspace (or an explicit allow-list) are reachable; opened with `mode=ro` URIs plus `PRAGMA query_only`; the driver rejects multi-statement strings; row cap.
- **Outbound approval** on by default (see 4.3).
- **Template rendering** in a Jinja2 sandbox.
- **Plugins are trusted code** — they run in-process with full privileges. The README says so plainly.

## 7. Key decisions (ADR log)

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| 1 | Host uses the MCP **client** over in-memory transport to reach its own tools | Call connector functions directly | Keeps one code path for internal and external clients and makes the Inspector show genuine JSON-RPC. Small overhead is irrelevant locally. |
| 2 | **mcp 2.x `MCPServer`** | Pin `mcp<2` and use `FastMCP` | 2.x is current; `add_tool`/`remove_tool` cover dynamic registration. |
| 3 | **Direct `httpx` adapters** for Anthropic / OpenAI / Ollama | LiteLLM; official vendor SDKs | LiteLLM (1.106) declares `Requires-Python <3.15`, so it cannot install on Python 3.15, and it is a very large dependency. The three APIs are small and stable; ~100 lines per adapter gives full control over streaming and tool calls, and the protocol makes adding providers easy. Trade-off: we maintain the adapters. |
| 4 | **SSE** over WebSockets | WebSockets | All real-time flows are server → browser; SSE works with HTMX's `sse` extension and plain `EventSource`, survives proxies, auto-reconnects. Browser → server stays plain POST. |
| 5 | **SQLite + aiosqlite**, no ORM | SQLAlchemy | Handful of tables; raw SQL with Pydantic models keeps it small and transparent. |
| 6 | Tailwind/HTMX/Alpine **via CDN** | Vite build | Zero-build requirement; versions pinned in URLs. Trade-off: needs internet on first page load for CDN assets (can be vendored later). |
| 7 | Approval gate inside the **server** (not the agent) | Gate only in the chat agent | Ensures external MCP clients can't bypass approval for outbound actions. |
| 8 | Custom **`TappedTransport`** for the host client | SDK's in-process `Client(server)` | The SDK's in-process path skips JSON-RPC entirely (direct dispatcher), so there would be nothing genuine to inspect. |
| 9 | Tool names `connector_tool` | `connector.tool` | Anthropic and OpenAI reject dots in tool names. |
| 10 | **selectolax `lexbor`** backend for HTML | BeautifulSoup, readability libs | Fast C parser with no extra deps; selectolax 1.0 removed the old `modest` backend. |
| 11 | External traffic tapped with **server middleware** | ASGI middleware parsing HTTP/SSE bodies | Transport-agnostic (covers HTTP *and* stdio), no body buffering or SSE parsing. Trade-off: envelopes are rebuilt from method/id/params/result rather than byte-copied. |
| 12 | **Approvals mediated by SQLite** | In-memory futures only | `nexus stdio` and `nexus serve` are separate processes; the DB row is the only shared state. Polling every 0.5 s is negligible locally. |
| 13 | **One SSE endpoint** (`/api/events`) with a topic filter | One endpoint per stream | Fewer connections from the dashboard (browsers cap HTTP/1.1 connections per host). Chat replies stream on their own POST response. |
| 14 | `--project-dir` CLI flag | Absolute paths in every setting | MCP clients launch `nexus stdio` from an arbitrary working directory; one flag makes `.env`, `data/`, `workspace/` and `plugins/` resolve as if run from the project. |
| 15 | Component CSS in **`@layer components`** | Unlayered stylesheet | Unlayered CSS beats Tailwind v4's layered utilities, so `w-56`/`w-auto` silently lost to `.input { width: 100% }`. Layering restores the intended order (base < components < utilities). |
| 16 | **HTMX for server-rendered lists, Alpine for live state** | All-Alpine SPA; all-HTMX | Connector cards and the conversation list are plain server data and benefit from one source of truth in Jinja; streaming chat, forms and the inspector need client state. |
