/* MCP Nexus dashboard — Alpine.js components.
 *
 *   nexus        app shell: tabs, theme, live event stream, approvals, toasts
 *   chat         conversations + streaming agent replies with tool-call rows
 *   connections  LLM providers, approval policy, webhook targets, connector settings
 *   studio       template editor, preview, run
 *   inspector    two-lane JSON-RPC timeline
 *
 * One EventSource (/api/events) feeds everything live; chat replies stream on
 * their own POST response.
 */

// ── helpers ──────────────────────────────────────────────────
const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem("nexus:" + key); return v === null ? fallback : JSON.parse(v); }
    catch { return fallback; }
  },
  set(key, value) { try { localStorage.setItem("nexus:" + key, JSON.stringify(value)); } catch { /* private mode */ } },
};

async function api(path, { method = "GET", body } = {}) {
  const res = await fetch(path, {
    method,
    headers: body !== undefined ? { "content-type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail
      : Array.isArray(data.detail) ? data.detail.map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`).join("; ")
      : res.statusText;
    throw new Error(detail || `Request failed (${res.status})`);
  }
  return data;
}

/** Parse a text/event-stream fetch response into {event, data} objects. */
async function* readSSE(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
    let cut;
    while ((cut = buffer.indexOf("\n\n")) >= 0) {
      const block = buffer.slice(0, cut);
      buffer = buffer.slice(cut + 2);
      let event = "message";
      const data = [];
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
      }
      if (data.length) {
        try { yield { event, data: JSON.parse(data.join("\n")) }; } catch { /* ignore keep-alives */ }
      }
    }
  }
}

const escapeHTML = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

/** Small, safe markdown subset: fenced code, inline code, bold, italics, links, headings, lists. */
function renderMarkdown(src) {
  const blocks = [];
  let text = String(src ?? "").replace(/```[^\n]*\n([\s\S]*?)(```|$)/g, (_, code) => {
    blocks.push(`<pre><code>${escapeHTML(code.replace(/\n$/, ""))}</code></pre>`);
    return `\u0000${blocks.length - 1}\u0000`;
  });
  text = escapeHTML(text)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const out = [];
  let list = null;
  for (const line of text.split("\n")) {
    const bullet = line.match(/^\s*[-*]\s+(.*)/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
    const heading = line.match(/^(#{1,3})\s+(.*)/);
    if (bullet || numbered) {
      const kind = bullet ? "ul" : "ol";
      if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${(bullet || numbered)[1]}</li>`);
      continue;
    }
    if (list) { out.push(`</${list}>`); list = null; }
    if (heading) out.push(`<h${heading[1].length + 1}>${heading[2]}</h${heading[1].length + 1}>`);
    else if (line.trim() === "") out.push("");
    else out.push(`<p>${line}</p>`);
  }
  if (list) out.push(`</${list}>`);
  return out.join("\n")
    .replace(/<\/p>\n<p>/g, "<br>")
    .replace(/<p>\u0000(\d+)\u0000<\/p>|\u0000(\d+)\u0000/g, (_, a, b) => blocks[Number(a ?? b)]);
}

/** Pretty-print JSON with token classes (keys teal, strings violet). */
function highlightJSON(value) {
  const json = escapeHTML(JSON.stringify(value, null, 2));
  return json.replace(
    /(&quot;(?:\\.|[^&]|&(?!quot;))*?&quot;)(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:e[+-]?\d+)?/gi,
    (match, str, colon, lit) => {
      if (str) return colon ? `<span class="k">${str}</span>${colon}` : `<span class="s">${str}</span>`;
      if (lit) return `<span class="b">${lit}</span>`;
      return `<span class="n">${match}</span>`;
    },
  );
}

function prettyArgs(value) {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") {
    try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
  }
  return JSON.stringify(value, null, 2);
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
const sentenceCase = (key) => key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

const timeOf = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });

document.addEventListener("alpine:init", () => {
  // ── app shell ──────────────────────────────────────────────
  Alpine.data("nexus", () => ({
    tab: store.get("tab", "chat"),
    theme: store.get("theme", "system"),
    settings: {},
    approvals: { pending: [], history: [], auto_approve: false, timeout_seconds: 120 },
    toasts: [],
    connected: false,
    source: null,

    init() {
      this.applyTheme();
      this.$watch("tab", (t) => store.set("tab", t));
      this.refreshSettings();
      this.refreshApprovals();
      this.connect();
      // Approvals raised by another process (e.g. `nexus stdio` for Claude Desktop) arrive via the DB.
      setInterval(() => this.refreshApprovals(), 4000);
    },

    go(tab) { this.tab = tab; },

    cycleTheme() {
      this.theme = { system: "light", light: "dark", dark: "system" }[this.theme];
      store.set("theme", this.theme);
      this.applyTheme();
    },
    applyTheme() {
      if (this.theme === "system") document.documentElement.removeAttribute("data-theme");
      else document.documentElement.setAttribute("data-theme", this.theme);
    },
    get themeLabel() { return { system: "Auto", light: "Light", dark: "Dark" }[this.theme]; },

    async refreshSettings() {
      try { this.settings = await api("/api/settings"); } catch (e) { this.toast(e.message, "error"); }
    },
    async refreshApprovals() {
      try {
        const before = new Set(this.approvals.pending.map((p) => p.id));
        this.approvals = await api("/api/approvals");
        for (const p of this.approvals.pending) {
          if (!before.has(p.id) && this.tab !== "chat") this.toast(`Waiting for your approval: ${p.summary}`, "hold");
        }
      } catch { /* server restarting */ }
    },

    connect() {
      this.source?.close();
      const es = new EventSource("/api/events?topics=rpc,approvals,connectors");
      this.source = es;
      es.onopen = () => { this.connected = true; };
      es.onerror = () => { this.connected = false; };
      es.addEventListener("rpc", (e) => window.dispatchEvent(new CustomEvent("nexus:rpc", { detail: JSON.parse(e.data) })));
      es.addEventListener("approvals", (e) => {
        const ev = JSON.parse(e.data);
        if (ev.type === "auto_approve") this.approvals.auto_approve = ev.value;
        this.refreshApprovals();
      });
      es.addEventListener("connectors", (e) => {
        const ev = JSON.parse(e.data);
        if (ev.type === "error") this.toast(`Connector ${ev.connector.name} failed to load.`, "error");
        window.dispatchEvent(new CustomEvent("nexus:connectors"));
      });
    },

    async decide(id, approve) {
      try {
        await api(`/api/approvals/${id}`, { method: "POST", body: { approve } });
        this.approvals.pending = this.approvals.pending.filter((p) => p.id !== id);
        this.toast(approve ? "Approved. Sending now." : "Denied. Nothing was sent.");
      } catch (e) {
        this.toast(e.message, "error");
        this.refreshApprovals();
      }
    },
    async setAutoApprove(value) {
      try {
        await api("/api/approvals/auto", { method: "PUT", body: { value } });
        this.approvals.auto_approve = value;
        this.toast(value ? "Outbound actions now send without asking." : "Outbound actions now wait for your approval.");
      } catch (e) { this.toast(e.message, "error"); }
    },
    pendingFor(toolName) { return this.approvals.pending.find((p) => p.tool === toolName); },

    toast(text, kind = "info") {
      const id = Math.random().toString(36).slice(2);
      this.toasts.push({ id, text, kind });
      setTimeout(() => { this.toasts = this.toasts.filter((t) => t.id !== id); }, kind === "error" ? 7000 : 4000);
    },
  }));

  // ── chat ───────────────────────────────────────────────────
  Alpine.data("chat", () => ({
    conversationId: store.get("conversation", null),
    messages: [],
    input: "",
    model: "",
    modelGroups: [],
    streaming: false,
    controller: null,
    useTools: true,
    showConvos: false,
    starters: [
      "How much memory and disk space is free on this computer?",
      "Read the files in my workspace and tell me what's there.",
      "Fetch https://modelcontextprotocol.io and summarise what MCP is in five bullets.",
    ],

    async init() {
      await this.loadModels();
      if (this.conversationId) this.open(this.conversationId);
      window.addEventListener("open-conversation", (e) => { this.showConvos = false; this.open(e.detail.id); });
      window.addEventListener("run-template", (e) => this.runTemplate(e.detail));
      // bring an approval card into view as soon as it appears
      this.$watch("approvals.pending.length", (n, old) => { if (n > (old || 0)) this.scroll(true); });
    },

    async loadModels() {
      try {
        const [byProvider, active] = await Promise.all([api("/api/llm/models"), api("/api/llm/active")]);
        this.modelGroups = Object.entries(byProvider).map(([provider, info]) => ({ provider, ...info }));
        this.model = active.model;
      } catch { this.modelGroups = []; }
    },
    get modelKnown() {
      return this.modelGroups.some((g) => g.models.some((m) => m.id === this.model));
    },
    async chooseModel() {
      try { await api("/api/llm/active", { method: "PUT", body: { model: this.model } }); } catch { /* keep local */ }
    },

    newChat() {
      this.stop();
      this.conversationId = null;
      store.set("conversation", null);
      this.messages = [];
      this.refreshList();
      this.$nextTick(() => this.$refs.input?.focus());
    },

    async open(id) {
      if (this.streaming) return;
      try {
        const convo = await api(`/api/conversations/${id}`);
        this.conversationId = id;
        store.set("conversation", id);
        this.messages = this.fromHistory(convo.messages);
        if (convo.model) this.model = convo.model;
        this.refreshList();
        this.scroll();
      } catch {
        this.conversationId = null;
        store.set("conversation", null);
      }
    },

    fromHistory(rows) {
      const out = [];
      const toolsById = {};
      let reply = null;
      for (const row of rows) {
        if (row.role === "user") {
          out.push({ role: "user", text: row.content });
          reply = null;
        } else if (row.role === "assistant") {
          if (!reply) {
            reply = { role: "assistant", parts: [], done: true, notices: [], error: null };
            out.push(reply);
          }
          if (row.content) reply.parts.push({ type: "text", text: row.content });
          for (const c of row.tool_calls || []) {
            const t = { type: "tool", id: c.id, name: c.name, arguments: c.arguments, result: null, is_error: false, state: "done" };
            toolsById[c.id] = t;
            reply.parts.push(t);
          }
        } else if (row.role === "tool" && toolsById[row.tool_call_id]) {
          const t = toolsById[row.tool_call_id];
          t.result = row.content;
          t.is_error = /^(Error executing tool|Action not performed|Tool call failed)/.test(row.content || "");
          t.state = t.is_error ? "error" : "done";
        }
      }
      return out;
    },

    refreshList() {
      htmx.ajax("GET", `/ui/conversations${this.conversationId ? `?active=${this.conversationId}` : ""}`, { target: "#convos-list", swap: "innerHTML" });
    },

    async remove() {
      if (!this.conversationId) return;
      await api(`/api/conversations/${this.conversationId}`, { method: "DELETE" });
      this.newChat();
    },

    send(text) {
      const message = (text ?? this.input).trim();
      if (!message || this.streaming) return;
      this.input = "";
      this.messages.push({ role: "user", text: message });
      this.scroll(true);
      this.stream("/api/chat", {
        message, conversation_id: this.conversationId, model: this.model || null, use_tools: this.useTools,
      });
    },

    runTemplate({ name, arguments: args }) {
      if (this.streaming) return;
      this.conversationId = null;
      store.set("conversation", null);
      this.messages = [];
      this.stream(`/api/templates/${encodeURIComponent(name)}/run`, { arguments: args, model: this.model || null });
    },

    async stream(url, body) {
      const reply = { role: "assistant", parts: [], done: false, notices: [], error: null };
      this.messages.push(reply);
      const r = this.messages[this.messages.length - 1];
      this.streaming = true;
      this.controller = new AbortController();
      this.scroll(true);
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: { "content-type": "application/json", accept: "text/event-stream" },
          body: JSON.stringify(body),
          signal: this.controller.signal,
        });
        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.detail || `Request failed (${res.status})`);
        }
        for await (const { event, data } of readSSE(res)) {
          if (event === "prompt") {
            this.messages.splice(this.messages.length - 1, 0, { role: "user", text: data.text, template: data.template });
          } else if (event === "conversation") {
            this.conversationId = data.id;
            store.set("conversation", data.id);
          } else if (event === "text") {
            const last = r.parts[r.parts.length - 1];
            if (last && last.type === "text") last.text += data.delta;
            else r.parts.push({ type: "text", text: data.delta });
          } else if (event === "tool_call") {
            r.parts.push({ type: "tool", id: data.id, name: data.name, arguments: data.arguments, result: null, is_error: false, state: "running" });
          } else if (event === "tool_result") {
            const t = r.parts.find((x) => x.type === "tool" && x.id === data.id);
            if (t) { t.result = data.content; t.is_error = data.is_error; t.state = data.is_error ? "error" : "done"; }
          } else if (event === "notice") {
            r.notices.push(data.message);
          } else if (event === "error") {
            r.error = data.message;
          }
          this.scroll();
        }
      } catch (e) {
        if (e.name !== "AbortError") r.error = e.message;
      } finally {
        r.done = true;
        for (const t of r.parts) if (t.type === "tool" && t.state === "running") t.state = "error";
        this.streaming = false;
        this.controller = null;
        this.refreshList();
        this.scroll();
      }
    },

    stop() { this.controller?.abort(); },

    onKey(e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); this.send(); }
    },

    toolState(t) {
      if (t.state === "running" && this.pendingFor(t.name)) return "waiting";
      return t.state;
    },
    isLastText(m, part) {
      const texts = m.parts.filter((p) => p.type === "text");
      return !m.done && texts[texts.length - 1] === part && m.parts[m.parts.length - 1] === part;
    },
    toolStateLabel(state) {
      return { running: "Running…", waiting: "Waiting for your approval", done: "Done", error: "Failed" }[state];
    },

    md: renderMarkdown,
    prettyArgs,

    scroll(force = false) {
      this.$nextTick(() => {
        const el = this.$refs.scroller;
        if (el && (force || el.scrollHeight - el.scrollTop - el.clientHeight < 240)) el.scrollTop = el.scrollHeight;
      });
    },
  }));

  // ── connections ────────────────────────────────────────────
  Alpine.data("connections", () => ({
    providers: [],
    drafts: {},
    tests: {},
    testModel: {},
    models: {},
    targets: [],
    newTarget: { name: "", kind: "discord", url: "", description: "" },
    config: null, // { name, schema, values, saving, error }

    async init() {
      await Promise.all([this.loadProviders(), this.loadTargets()]);
      window.addEventListener("open-config", (e) => this.openConfig(e.detail.name));
      window.addEventListener("nexus:connectors", () => htmx.trigger("#connectors", "refresh"));
    },

    async loadProviders() {
      this.providers = await api("/api/llm/providers");
      for (const p of this.providers) {
        this.drafts[p.name] ??= { api_key: "", base_url: p.base_url_source === "ui" ? p.base_url : "" };
      }
      this.models = await api("/api/llm/models").catch(() => ({}));
    },

    async saveProvider(p) {
      const draft = this.drafts[p.name];
      try {
        await api(`/api/llm/providers/${p.name}`, {
          method: "PUT",
          body: { api_key: draft.api_key || null, base_url: draft.base_url },
        });
        draft.api_key = "";
        await this.loadProviders();
        this.test(p);
      } catch (e) { this.tests[p.name] = { ok: false, error: e.message }; }
    },

    async clearKey(p) {
      await api(`/api/llm/providers/${p.name}`, { method: "PUT", body: { clear_key: true } });
      await this.loadProviders();
    },

    async test(p) {
      this.tests[p.name] = { running: true };
      try {
        this.tests[p.name] = await api(`/api/llm/providers/${p.name}/test`, {
          method: "POST", body: { model: this.testModel[p.name] || null },
        });
      } catch (e) { this.tests[p.name] = { ok: false, error: e.message }; }
    },

    plural,
    keySourceLabel(p) {
      return { env: "from .env", ui: "saved here", none: "not set" }[p.api_key_source];
    },

    async loadTargets() { this.targets = await api("/api/targets"); },
    async addTarget() {
      const t = this.newTarget;
      try {
        await api(`/api/targets/${encodeURIComponent(t.name.trim())}`, {
          method: "PUT", body: { kind: t.kind, url: t.url.trim(), description: t.description },
        });
        this.newTarget = { name: "", kind: t.kind, url: "", description: "" };
        await this.loadTargets();
      } catch (e) { this.targetError = e.message; }
    },
    targetError: "",
    async removeTarget(name) {
      await api(`/api/targets/${encodeURIComponent(name)}`, { method: "DELETE" });
      await this.loadTargets();
    },

    // connector settings form generated from the Config model's JSON schema
    async openConfig(name) {
      const data = await api(`/api/connectors/${name}/config`);
      const fields = Object.entries(data.schema?.properties || {}).map(([key, prop]) => {
        const branch = prop.anyOf ? prop.anyOf.find((b) => b.type !== "null") || {} : prop;
        let kind = branch.type || "string";
        if (branch.format === "password" || branch.writeOnly) kind = "secret";
        if (kind === "array") kind = "list";
        return { key, kind, label: sentenceCase(key), help: prop.description || "", nullable: !!prop.anyOf };
      });
      const values = { ...data.values };
      for (const f of fields) if (f.kind === "list") values[f.key] = (values[f.key] || []).join("\n");
      this.config = { name, fields, values, error: "", saving: false };
    },
    async saveConfig() {
      const c = this.config;
      c.saving = true;
      c.error = "";
      const body = {};
      for (const f of c.fields) {
        let v = c.values[f.key];
        if (f.kind === "list") v = String(v || "").split("\n").map((x) => x.trim()).filter(Boolean);
        else if (f.kind === "integer" || f.kind === "number") v = v === "" || v === null ? (f.nullable ? null : 0) : Number(v);
        else if (f.kind === "boolean") v = !!v;
        body[f.key] = v;
      }
      try {
        await api(`/api/connectors/${c.name}/config`, { method: "PUT", body });
        this.config = null;
        htmx.trigger("#connectors", "refresh");
      } catch (e) { c.error = e.message; } finally { if (this.config) this.config.saving = false; }
    },
  }));

  // ── template studio ────────────────────────────────────────
  Alpine.data("studio", () => ({
    templates: [],
    selected: null,
    form: null,
    variables: [],
    syntaxError: "",
    values: {},
    preview: "",
    previewError: "",
    saving: false,
    status: "",
    timer: null,

    async init() { await this.load(); },

    async load(selectName) {
      this.templates = await api("/api/templates");
      const pick = selectName || this.selected || this.templates[0]?.name;
      if (pick) this.select(pick);
    },

    select(name) {
      const t = this.templates.find((x) => x.name === name);
      if (!t) return;
      this.selected = name;
      this.form = {
        name: t.name, description: t.description, body: t.body,
        arguments: t.arguments.map((a) => ({ ...a, default: a.default ?? "" })),
        isNew: false,
      };
      this.values = {};
      this.preview = "";
      this.previewError = "";
      this.status = "";
      this.validate();
    },

    create() {
      this.selected = null;
      this.form = { name: "", description: "", body: "", arguments: [], isNew: true };
      this.variables = [];
      this.values = {};
      this.preview = "";
      this.status = "";
      this.$nextTick(() => this.$refs.tplName?.focus());
    },

    onBodyInput() {
      clearTimeout(this.timer);
      this.timer = setTimeout(() => this.validate(), 300);
    },

    async validate() {
      if (!this.form) return;
      const res = await api("/api/templates/validate", { method: "POST", body: { body: this.form.body } });
      this.syntaxError = res.ok ? "" : res.error;
      if (!res.ok) return;
      this.variables = res.variables;
      const known = new Set(this.form.arguments.map((a) => a.name));
      for (const v of res.variables) {
        if (!known.has(v)) this.form.arguments.push({ name: v, description: "", required: true, default: "" });
      }
      this.form.arguments = this.form.arguments.filter((a) => res.variables.includes(a.name));
    },

    async save() {
      this.saving = true;
      this.status = "";
      try {
        const f = this.form;
        await api(`/api/templates/${encodeURIComponent(f.name)}`, {
          method: "PUT",
          body: {
            name: f.name, description: f.description, body: f.body,
            arguments: f.arguments.map((a) => ({ ...a, default: a.default === "" ? null : a.default })),
          },
        });
        this.status = "Saved. It's also available as an MCP prompt.";
        await this.load(f.name);
      } catch (e) { this.status = e.message; } finally { this.saving = false; }
    },

    async remove() {
      if (!this.selected) return;
      await api(`/api/templates/${encodeURIComponent(this.selected)}`, { method: "DELETE" });
      this.selected = null;
      this.form = null;
      await this.load();
    },

    filledArgs() {
      return Object.fromEntries(Object.entries(this.values).filter(([, v]) => v !== "" && v != null));
    },

    async renderPreview() {
      this.previewError = "";
      try {
        const res = await api(`/api/templates/${encodeURIComponent(this.selected)}/render`, {
          method: "POST", body: { arguments: this.filledArgs() },
        });
        this.preview = res.text;
      } catch (e) { this.preview = ""; this.previewError = e.message; }
    },

    run() {
      window.dispatchEvent(new CustomEvent("run-template", { detail: { name: this.selected, arguments: this.filledArgs() } }));
      this.go("chat"); // from the parent `nexus` scope
    },
  }));

  // ── inspector ──────────────────────────────────────────────
  Alpine.data("inspector", () => ({
    entries: [],
    paused: false,
    buffered: 0,
    queue: [],
    filter: "",
    transport: "all",
    selected: null,
    follow: true,
    max: 2000,

    async init() {
      try { this.entries = await api("/api/inspector/history?limit=500"); } catch { this.entries = []; }
      window.addEventListener("nexus:rpc", (e) => this.push(e.detail));
      this.$nextTick(() => this.toBottom());
    },

    push(entry) {
      if (this.paused) { this.queue.push(entry); this.buffered = this.queue.length; return; }
      this.entries.push(entry);
      if (this.entries.length > this.max) this.entries.splice(0, this.entries.length - this.max);
      if (this.follow) this.toBottom();
    },

    togglePause() {
      this.paused = !this.paused;
      if (!this.paused) {
        for (const e of this.queue) this.entries.push(e);
        this.queue = [];
        this.buffered = 0;
        this.toBottom();
      }
    },

    get visible() {
      const q = this.filter.trim().toLowerCase();
      return this.entries.filter((e) =>
        (this.transport === "all" || e.transport === this.transport)
        && (!q || (e.method || "").toLowerCase().includes(q) || JSON.stringify(e.payload).toLowerCase().includes(q)));
    },

    key(e) { return `${e.session}|${e.rpc_id}`; },
    isPair(e) { return this.selected && e !== this.selected && e.rpc_id != null && this.key(e) === this.key(this.selected); },
    partner(e) {
      if (!e || e.rpc_id == null) return null;
      return this.entries.find((x) => x !== e && this.key(x) === this.key(e) && x.direction !== e.direction) || null;
    },

    showSession(e, i) {
      const list = this.visible;
      return i === 0 || list[i - 1].session !== e.session || list[i - 1].transport !== e.transport;
    },

    label(e) {
      if (e.method) return e.method;
      return e.kind === "error" ? "error" : "result";
    },
    toolName(e) {
      const p = e.payload?.params;
      if (p?.name) return p.name;
      if (p?.uri) return p.uri;
      return "";
    },
    kindLabel(e) {
      return { request: "Request", response: "Response", notification: "Notification", error: "Error" }[e.kind];
    },
    dirLabel(e) { return e.direction === "c2s" ? "Client → server" : "Server → client"; },
    transportLabel(t) { return { inproc: "This dashboard", http: "HTTP client", stdio: "stdio client" }[t] || t; },
    time: timeOf,
    json: highlightJSON,

    async clear() {
      await api("/api/inspector/history", { method: "DELETE" });
      this.entries = [];
      this.queue = [];
      this.selected = null;
    },

    exportJSON() {
      const blob = new Blob([JSON.stringify(this.visible, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `mcp-nexus-rpc-${new Date().toISOString().slice(0, 19).replace(/:/g, "")}.json`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    },

    onScroll() {
      const el = this.$refs.wire;
      this.follow = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    },
    toBottom() {
      this.$nextTick(() => { const el = this.$refs.wire; if (el) el.scrollTop = el.scrollHeight; });
    },
  }));
});
