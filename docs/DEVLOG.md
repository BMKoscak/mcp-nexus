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

- **Dropped LiteLLM in favour of direct `httpx` provider clients** (ADR 3, revised during Phase 1). Verifying the install on the dev machine (Windows, Python 3.15.0) failed: every LiteLLM release up to 1.106.4 declares `Requires-Python <3.15`. All other dependencies (`mcp` 2.3.0, FastAPI 0.162, Pydantic 2.14, cryptography, selectolax, psutil…) install cleanly on 3.15. The brief allowed "LiteLLM or direct async clients"; direct clients remove the version ceiling and a large dependency tree, at the cost of maintaining three small adapters (Anthropic Messages, OpenAI Chat Completions, Ollama `/api/chat`) behind one `Provider` protocol.

### Verified
- Windows / Python 3.15.0, fresh venv: `pip install -e ".[dev]"` succeeds, `pytest` → 2 passed, `ruff check` → clean.
- Both Mermaid diagrams in `ARCHITECTURE.md` render with mermaid-cli.
- Added `.gitattributes` (LF in the repo) so Windows checkouts don't produce CRLF churn.

### How it fits together (so far)
Only configuration is executable at this point. `Settings` is the single source for paths (`data_dir`, `workspace_dir`, `plugins_dir`) and limits that every later module reads via `get_settings()`.

### Next (Phase 2)
`db.py` + migrations, `secrets.py`, `events.py` bus, `mcp_engine.py` (server, registry, RPC tap, in-memory client), the `Connector` base class, and the Filesystem, Outbound (webhook/email) and Web Fetcher connectors with tests.

---

## Phase 2 — MCP core, persistence & connectors · 2026-10-25

### Built
| Module | What it does |
|---|---|
| `nexus/db.py` | aiosqlite database with `PRAGMA user_version` migrations (v1: settings, providers, connector_state, webhook_targets, templates, conversations, messages, rpc_log, pending_actions), WAL mode, and repository methods so no other module writes SQL. |
| `nexus/crypto.py` | `SecretBox` (Fernet) for API keys, webhook URLs and connector secrets; key from `NEXUS_SECRET_KEY` or auto-generated `data/secret.key`; `mask()` for display. Named `crypto` rather than `secrets` to avoid shadowing the stdlib module. |
| `nexus/events.py` | Non-blocking pub/sub (`rpc`, `approvals`, `connectors` topics); slow subscribers drop their oldest events instead of stalling MCP traffic. |
| `nexus/approvals.py` | `ApprovalGate`: per-action future, persisted audit trail, timeout → denied, persistent auto-approve toggle. |
| `nexus/mcp_engine.py` | `MCPEngine` (server + registry + tap + host client), `TappedTransport`, `RpcTap`. |
| `nexus/registry.py` | Connector discovery (built-ins, `plugins/*.py`, `mcp_nexus.connectors` entry points), enable/disable, hot plugin reload, config forms with encrypted secrets, approval wrapping. |
| `nexus/connectors/base.py` | Public plugin API: `Connector`, `@tool`, `@resource`, `ToolError`. |
| `nexus/netguard.py` | SSRF guard used by the web fetcher. |
| Connectors | **filesystem** (6 tools + tree resource), **outbound** (webhook, Discord, Slack, email, ntfy push, list targets), **web** (fetch → markdown/text/html, extract links, fetch JSON), **system** (stats, top processes + stats resource), **sqlite** (list DBs/tables, describe, read-only query). 21 tools, 2 resources. |
| Tests | 50 tests: core (db/crypto/bus/gate), engine (registration metadata, tap pairing + latency, enable/disable persistence across restarts, plugin discovery/reload/broken plugin, encrypted config, approve/deny/auto paths with a mocked HTTP transport) and connectors (sandbox escapes, SSRF incl. redirect-to-localhost, truncation, HTML→markdown, ntfy, SQL write rejection). |

### Decisions & findings
- **The SDK's in-process client has no JSON-RPC.** Passing an `MCPServer` straight to `mcp.client.Client` in 2.x uses a *direct dispatcher*: no framing, no `server/discover`, nothing for an inspector to see. I prototyped a `TappedTransport` (two memory-stream pairs + relay tasks) and confirmed the full protocol flows through it: `server/discover`, `tools/list`, `tools/call`, results with `_meta`, protocol `2026-07-28` (ADR 8).
- **Tool names use `_`, not `.`**: Anthropic and OpenAI both restrict tool names to `[A-Za-z0-9_-]{1,64}` (ADR 9).
- **Return `dict[str, Any]`, not bare `dict`**: the SDK only generates an output schema and `structuredContent` for parameterised dicts. That surfaced as three test failures and is now used for every connector.
- **Supervisor task for the client session**: the first full test run hung because pytest-asyncio runs fixture setup and teardown in different tasks, and anyio task groups must exit in the task that entered them. Running the session in its own task fixes that and will matter again under FastAPI's lifespan.
- **`functools.wraps` for approval gating** keeps the generated input schema identical, which I verified before relying on it.
- **selectolax 1.0 removed the `modest` backend**; switched to `selectolax.lexbor` (same API) (ADR 10).
- **Masked errors**: `ToolError` messages go to the model verbatim; unexpected exceptions are masked by the SDK, so tracebacks never reach the LLM.
- **Deferred:** editing arguments on the approval card (would bypass validation); a public `remove_resource` (the registry pops from the resource manager's dict until the SDK has one).

### How it fits together
```
FastAPI (Phase 3) ──► MCPEngine.call_tool()
                         │  host Client (supervisor task)
                         ▼
                    TappedTransport ──► RpcTap ──► EventBus "rpc" ──► Inspector SSE (Phase 4)
                         │                    └──► rpc_log (batched)
                         ▼
                     MCPServer ◄── ConnectorRegistry ◄── built-ins / plugins / entry points
                         │
             gated tool? ├──► ApprovalGate ──► EventBus "approvals" ──► UI card (Phase 4)
                         ▼
                   Connector method (ctx: settings, db, crypto, bus, approvals, http)
```

### Verified
- Cloud (Linux, Python 3.13): 50 passed; `ruff check` and `ruff format --check` clean.
- Windows / Python 3.15.0: 49 passed, 1 skipped (symlink-escape test needs admin rights on Windows).

### Next (Phase 3)
`main.py` app factory + lifespan, `/mcp` Streamable HTTP mount (tapped via ASGI middleware) and `nexus stdio`, provider adapters + `llm_router.py`, the agent tool-calling loop, the template manager (Jinja2 sandbox → MCP prompts), and REST/SSE routes.

---

## Phase 3 — FastAPI backend, LLM router, agent & templates · 2026-10-25

### Built
| Module | What it does |
|---|---|
| `nexus/providers/` | Provider-neutral types (`Message`, `ToolDef`, `ToolCall`, stream events `TextDelta`/`ToolCallEvent`/`Done`) and three `httpx` adapters: **Anthropic** Messages (SSE, `tool_use`/`tool_result` blocks, turn merging), **OpenAI** Chat Completions (SSE, tool-call fragment reassembly, usage; doubles as an OpenAI-compatible client for LM Studio/vLLM/OpenRouter), **Ollama** `/api/chat` (NDJSON, native tools). |
| `nexus/llm_router.py` | Credentials (env → encrypted SQLite), masked provider listing with key source, parallel live model discovery with per-provider errors, connection test with a "pong" completion, `provider/model` routing, persisted active model. |
| `nexus/agent.py` | Tool-calling loop over the tapped MCP client: streams text, runs tool calls (approval-gated ones pause), feeds results back, persists the conversation, retries without tools for models that can't use them, caps at 8 steps. |
| `nexus/templates.py` | Template CRUD with argument inference, Jinja2 `SandboxedEnvironment` + `StrictUndefined`, registration as MCP prompts, three seeded starters. |
| `nexus/main.py` | App factory + lifespan (engine → router → templates → Streamable HTTP session manager), error mapping (`ProviderError`→502, `KeyError`→404, `ValueError`→400), `/mcp` routes, static/index. |
| `nexus/api/` | 32 routes in six routers: system (health, settings, `/api/events` SSE, inspector history), llm, connectors (+ raw tools/resources/prompts, webhook targets), approvals, templates (validate, render, run-as-SSE), chat (SSE) + conversations. |
| `nexus/cli.py` | `nexus serve`, `nexus stdio`, `nexus tools`, `--project-dir`. |
| `ExternalTap` | Server middleware logging Streamable HTTP and stdio clients in the Inspector format. |
| Tests | 74 total (+24): provider adapters against canned vendor streams, and end-to-end API tests with a scripted `FakeProvider` (full tool loop writing a real file, conversation resume, approval during a chat, encrypted/masked keys, connector toggles and config validation, template CRUD/render/run/sandbox escape) plus a real uvicorn server hit by an external MCP client over HTTP. |

### Decisions & findings
- **External traffic via `ServerMiddleware`** (ADR 11). `mcp` 2.x has a context-tier middleware hook that sees every inbound request on any transport, so one class covers both `/mcp` and stdio without parsing HTTP or SSE bodies. The host's own in-process traffic is skipped there (it's already tapped on the wire).
- **Approvals now go through SQLite** (ADR 12). The Phase 2 gate kept futures in memory, which can't work when Claude Desktop runs `nexus stdio` as a separate process from the UI. `request()` now polls its `pending_actions` row and `decide()` does a first-writer-wins conditional UPDATE. A new test runs two gates on two connections to one database file to prove the cross-process path. `decide()` and `pending()` became async.
- **Schema migration v2** adds `rpc_log.session`. Caught by the HTTP test: the column was missing, and the migration runner handled the upgrade.
- **Route splicing instead of `app.mount`** for `/mcp`: a mount 307-redirects `/mcp` → `/mcp/`, which some clients don't follow for POST.
- **Lifespan in a single task in tests**: the same anyio cross-task rule as in Phase 2, now for the session manager's task group. The `app` fixture runs the lifespan in a dedicated task, which is what uvicorn does.
- **The new protocol needs an `mcp-method` header** on Streamable HTTP POSTs; a hand-written curl without it gets `-32020`. Real SDK clients send it.
- **ruff B008** flags FastAPI's `Depends()` defaults; configured `extend-immutable-calls` rather than contorting signatures.
- **Not verified against live LLMs**: there's no Ollama install or API key on the dev machine, so the adapters are verified against canned streams built from each vendor's documented format. A live check is on the Phase 5 list once a key or local model is available.

### How it fits together
```
Browser ──POST /api/chat──► Agent.run ──► LLMRouter ──► Anthropic | OpenAI | Ollama (httpx, streaming)
   ▲                           │
   │ SSE text/tool events      └─tool calls─► MCPEngine.call_tool ─► TappedTransport ─► MCPServer ─► connectors
   │                                                                     │                  ▲
   └──── GET /api/events (rpc · approvals · connectors) ◄── EventBus ◄───┘                  │
                                                                         ExternalTap ◄──────┤
Claude Desktop ── stdio (nexus stdio) ───────────────────────────────────────────────────────┤
Cursor etc.    ── Streamable HTTP POST /mcp ─────────────────────────────────────────────────┘
Approvals: pending_actions row in SQLite ◄── decided by POST /api/approvals/{id} (any process)
```

### Verified
- Cloud (Linux, Python 3.13): 74 passed; ruff check + format clean.
- Windows / Python 3.15.0: 73 passed, 1 skipped; `nexus serve` started, `/api/health` OK, an external MCP client listed 21 tools and called `system_stats` over `http://localhost:8765/mcp`, and the Inspector history showed the HTTP-transport rows.
- `nexus --project-dir … stdio` driven by the SDK's stdio client: 21 tools and 3 prompts listed, file written into the workspace, `prompts/get` rendered, 10 stdio rows logged.
- `/api/events` streamed live `rpc` events while a tool was called through the REST API.

### Next (Phase 4)
The dashboard: Chat & Execution (streaming, tool-call cards, inline approvals), Connections (LLM Hub + connector toggles/config forms + webhook targets), Template Studio, and the Live JSON-RPC Inspector, built with Jinja2 + Tailwind (CDN) + HTMX + Alpine.js on the endpoints above.

---

## Phase 4 — Dashboard · 2026-10-25

### Built
| File | What it does |
|---|---|
| `web/templates/index.html` | The single-page dashboard: navigation rail (bottom bar on phones) with an approvals badge, and four views: **Chat**, **Connect**, **Templates**, **Inspector**. Tailwind v4 browser build, HTMX 2.0.11 and Alpine 3.17.4 from jsdelivr, fonts from Google Fonts. Versions pinned, all published more than two weeks before use. |
| `web/templates/partials/` | HTMX fragments: connector cards (on/off switch swaps just that card), connector list, conversation list. |
| `web/static/app.js` | Alpine components `nexus` (shell, theme, one shared `EventSource`, approvals, toasts), `chat` (streaming SSE reader over `fetch`, ordered reply parts, history rebuild), `connections` (providers, destinations, schema-driven settings modal), `studio` (template editor, live variable detection, preview, run in chat), `inspector` (two-lane wire, pairing, filter, pause, export). Plus a small safe markdown renderer and a JSON highlighter. |
| `web/static/app.css` | Design tokens with automatic + manual dark mode; components in `@layer components`. |
| `nexus/api/ui.py` | Fragment routes: `/ui/connectors`, `/ui/connectors/{name}/toggle`, `/ui/connectors/reload`, `/ui/conversations`. |
| Tests | +4 (78 total): dashboard wiring, connector toggle fragments, conversation fragment, host-only destination masking. |

### Design
- **Subject:** a developer's private control room for MCP on their own machine. The one bold element is the Inspector's **two-lane wire**, with requests in teal left of a spine and replies in violet right of it. The same two colours then mean the same thing everywhere (tool rows in chat are a teal-over-violet stripe once a result returns). Amber appears only when something is waiting for you.
- Everything else stays quiet: cool paper-blue surfaces, an ink-navy rail, Instrument Sans for UI, a monospace face only where the content is code or JSON, sentence-case labels, no decorative cards-everywhere treatment.
- Copy is written from the user's side: "Local tools" not "connectors registry", "Destinations" not "webhook targets", "Send without asking" not "auto-approve outbound".

### Decisions & findings (from screenshot review)
I drove every screen with Playwright (light, dark, 390 px phone) using a scripted demo model so real tool flows, approvals and the inspector could be reviewed, then fixed what the screenshots showed:
- **Tailwind utilities were losing to my CSS** (ADR 15): unlayered CSS beats Tailwind v4's layered utilities, so `w-56`/`w-auto` were ignored and the Inspector toolbar stacked into three rows. Moving component CSS into `@layer components` fixed it.
- **User bubbles had a big empty top**: `white-space: pre-wrap` on the bubble preserved the template's indentation; moved it to the inner text span.
- **List bullets were missing** (Tailwind preflight resets `list-style`); restored them for rendered markdown.
- **New messages and approval cards could land off-screen**: sending now always scrolls to the bottom, and a new pending approval scrolls into view.
- **Toasts covered the Send button**: moved to the top right.
- **Destination masking leaked part of the secret**: Discord and Slack webhook URLs carry their token at the end, which the old "first 6 + last 6 characters" mask showed. Destinations now show only the host (`discord.com/…`), with a test.
- **Phones**: the conversation list was unreachable (now an overlay opened from a Chats button), and Inspector packets were cramped (timestamps hidden under 640 px).
- **Assistant replies are ordered parts** (text → tool → text), so a model that talks, calls a tool and then answers reads in the right order, live and when reloaded from history.
- No console errors or warnings in any of the runs; no horizontal scroll at 390 px.

### How it fits together
```
index.html ── x-data="nexus" ──► EventSource /api/events (rpc · approvals · connectors)
   │                                   ├─ rpc ───────► window "nexus:rpc" ─► inspector
   │                                   ├─ approvals ─► refresh pending ─► chat cards + rail badge
   │                                   └─ connectors ► htmx.trigger(#connectors, refresh)
   ├─ chat ──────── fetch POST /api/chat ──► SSE parts (text / tool_call / tool_result)
   ├─ connections ─ /api/llm/*, /api/targets, /api/approvals/auto, schema-driven settings
   │                └─ HTMX: /ui/connectors (+ toggle swaps one card)
   ├─ studio ────── /api/templates/* ─► "Run in chat" dispatches run-template to chat
   └─ inspector ─── /api/inspector/history + live nexus:rpc events
```

### Verified
- Cloud: 78 tests passed; ruff clean; Playwright walkthrough (chat with three tool flows incl. an approval, connections, settings modal, connector toggle, template preview, inspector selection; dark mode; phone) with no console errors. Screenshots saved to `docs/screenshots/`.

### Next (Phase 5)
README (badges, value proposition, Mermaid diagram, quickstart, Claude Desktop setup, 20-line connector guide, screenshots), final endpoint verification on Windows, create `BMKoscak/mcp-nexus` on GitHub and push.

---

## Phase 5 — Documentation, verification & release · 2026-10-25

### Built
- **`README.md`**: value proposition, badges (Python 3.12+, MCP 2026-07-28, mcp SDK 2.x, FastAPI, MIT), feature table, screenshots, Mermaid architecture diagram, quickstart for Windows/macOS/Linux, Claude Desktop and HTTP-client setup, the connector guide (a complete plugin in 13 lines plus an "everything" example with config, secrets, errors, approvals and resources), configuration table, API overview, security model, development notes and roadmap.
- **`scripts/smoke_test.py`**: 54 live checks against a running server (every REST route, HTMX fragments, static assets, OpenAPI docs, connector toggling, path-escape refusal, URL masking, template render through MCP `prompts/get`, conversation CRUD, the SSE event stream, Streamable HTTP MCP, and a real chat turn when a model is configured). Cleans up after itself; non-zero exit on failure.
- **`.github/workflows/ci.yml`** (written, not yet in the repo; see below): lint + tests on Ubuntu and Windows × Python 3.12/3.13.

### Verified
| Where | Result |
|---|---|
| Linux, Python 3.12.15 | 78 passed |
| Linux, Python 3.13 | 78 passed; ruff check + format clean; smoke test 54/54 |
| Windows, Python 3.15.0 | 77 passed, 1 skipped (symlink test needs admin); ruff clean; smoke test 54/54 against `nexus serve` |
| README examples | Both connector examples copied verbatim into a plugins folder; `nexus tools` lists `dice_roll`, `weather_now`, `weather_send_sms ⚠ approval` |
| Dashboard assets | All four CDN URLs (Tailwind, HTMX, Alpine, Google Fonts) return 200 from the Windows machine |

### Decisions & findings
- **ruff now formats Python code blocks inside Markdown.** `ruff format --check .` flagged the README and ARCHITECTURE examples; they're formatted, and the README example was reworded so a trailing comment didn't force an awkward wrap.
- **The CI workflow isn't committed yet.** The tool that writes into the project folder treats `.github/workflows/` as protected (workflow files execute code on GitHub), so it was delivered to Ben to add by hand, and the README's CI badge was left out until it exists.
- **Not verified against a live LLM.** There's still no API key or Ollama install on the dev machine. The provider adapters are covered by canned-stream tests built from each vendor's documented formats, and the smoke test automatically runs a real chat turn once a model is configured.

### Release
- Repository: **https://github.com/BMKoscak/mcp-nexus** (public, MIT), `main` branch, full commit history from Phase 1.

---

## v0.2 — Google Gemini provider · 2026-10-25

### Built
- `nexus/providers/gemini.py`: native adapter for the Generative Language API. It streams `streamGenerateContent?alt=sse` with `x-goog-api-key` auth, handles text and function-call parts, reports usage, surfaces blocked prompts and stream errors, and lists only `generateContent`-capable models (following `nextPageToken`).
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) in settings, `.env.example`, the router and the dashboard (the provider card appears automatically).
- `ToolCall.extra` + `to_record()`: provider-specific data persisted with the conversation but kept out of browser events.
- 7 new tests (85 total): the stream with a thought part, a signed call and an unsigned parallel call; signature and id replay and FC1, FC2, FR1, FR2 ordering; schema cleaning; model filtering and pagination; error surfacing; signatures surviving a SQLite round trip through the agent; the provider showing up in the API.

### Decisions & findings
- **Native API over the OpenAI-compatible endpoint** (ADR 17). Checked against Google's current docs: Gemini 3/4 attach a `thoughtSignature` to function-call parts and reject the next request with HTTP 400 if it isn't returned on the same part. With parallel calls only the first part is signed, and the replay must keep all calls before all responses. Doing this natively keeps it explicit and testable. Google's newer Interactions API was skipped because it's server-stateful, unlike every other adapter here.
- **`parametersJsonSchema` instead of `parameters`**: the latter accepts only an OpenAPI subset and rejects keywords MCP tools commonly emit (type arrays, nullable unions). Two gotchas from other projects' reports were handled: omit the key for parameterless tools (a `null` or empty schema is rejected), and inline `$ref`s when dropping `$defs`.
- **Not verified against the live API**: no Gemini key on the dev machine. Request and response shapes follow Google's API reference and thought-signature guide; the smoke test will do a real chat turn once a key is added.

Sources: [Gemini thought signatures](https://ai.google.dev/gemini-api/docs/generate-content/thought-signatures), [generateContent reference](https://ai.google.dev/api/generate-content), [models.list](https://ai.google.dev/api/models), [symfony/ai#2633](https://github.com/symfony/ai/pull/2633) on `parametersJsonSchema`.
