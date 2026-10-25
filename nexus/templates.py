"""Template Studio backend: parameterised prompt templates exposed as MCP prompts.

Bodies use Jinja2 syntax (``{{ url }}``, ``{{ tone | default('neutral') }}``,
``{% if %}``) rendered in a ``SandboxedEnvironment`` with ``StrictUndefined``, so
templates can't reach Python internals and a missing variable is an error, not
an empty string. Every saved template is registered on the MCP server, so it
shows up in ``prompts/list`` for external clients too.
"""

from __future__ import annotations

import re
from typing import Any

from jinja2 import StrictUndefined, TemplateError, meta
from jinja2.sandbox import SandboxedEnvironment
from mcp.server.mcpserver.prompts.base import Prompt, PromptArgument
from pydantic import BaseModel, Field, field_validator

from nexus.db import Database
from nexus.mcp_engine import MCPEngine

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SEEDED_KEY = "templates_seeded"


class TemplateArgument(BaseModel):
    name: str
    description: str = ""
    required: bool = True
    default: str | None = None


class TemplateIn(BaseModel):
    name: str = Field(description="lowercase, digits, - and _")
    description: str = ""
    body: str
    arguments: list[TemplateArgument] | None = Field(
        None, description="Omit to infer every variable in the body as a required argument."
    )

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError("name must be lowercase letters, digits, '-' or '_' (max 64)")
        return v


STARTER_TEMPLATES = [
    TemplateIn(
        name="summarize-url",
        description="Fetch a web page and summarise it.",
        body=(
            "Fetch {{ url }} with the web_fetch tool and summarise it as "
            "{{ style | default('5 concise bullet points') }}. "
            "Finish with one line on why it matters."
        ),
        arguments=[
            TemplateArgument(name="url", description="Page to summarise"),
            TemplateArgument(
                name="style", description="Output format", required=False, default=None
            ),
        ],
    ),
    TemplateIn(
        name="system-health-report",
        description="Check this machine's health and optionally notify you.",
        body=(
            "Use system_stats and system_top_processes to write a short health report for "
            "this computer: CPU, memory, disks over {{ disk_threshold | default('85') }}% full, "
            "and the top memory users.{% if notify == 'yes' %} Then send the summary as a push "
            "notification with outbound_notify.{% endif %}"
        ),
        arguments=[
            TemplateArgument(name="disk_threshold", description="Disk % to flag", required=False),
            TemplateArgument(
                name="notify", description="'yes' to send a notification", required=False
            ),
        ],
    ),
    TemplateIn(
        name="save-research-notes",
        description="Research a topic on the web and save notes to the workspace.",
        body=(
            "Research {{ topic }} using web_fetch on {{ sources | default('2-3 reputable') }} "
            "sources, then write well-structured markdown notes with a source list to "
            "notes/{{ filename }}.md using filesystem_write_file."
        ),
        arguments=[
            TemplateArgument(name="topic"),
            TemplateArgument(name="filename", description="File name without .md"),
            TemplateArgument(name="sources", required=False),
        ],
    ),
]


class TemplateManager:
    def __init__(self, engine: MCPEngine, db: Database) -> None:
        self.engine = engine
        self.db = db
        self.env = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)

    async def load_all(self, *, seed: bool = True) -> None:
        if seed and not await self.db.get_setting(SEEDED_KEY, False):
            for tpl in STARTER_TEMPLATES:
                if await self.db.get_template(tpl.name) is None:
                    await self.save(tpl)
            await self.db.set_setting(SEEDED_KEY, True)
        for row in await self.db.list_templates():
            self._register(row)

    # ── CRUD ─────────────────────────────────────────────────
    def variables(self, body: str) -> list[str]:
        try:
            ast = self.env.parse(body)
        except TemplateError as exc:
            raise ValueError(f"Template syntax error: {exc}") from None
        return sorted(meta.find_undeclared_variables(ast))

    async def save(self, tpl: TemplateIn) -> dict[str, Any]:
        found = self.variables(tpl.body)
        if tpl.arguments is None:
            args = [TemplateArgument(name=v) for v in found]
        else:
            args = list(tpl.arguments)
            declared = {a.name for a in args}
            args += [TemplateArgument(name=v) for v in found if v not in declared]
        await self.db.upsert_template(
            tpl.name,
            body=tpl.body,
            description=tpl.description,
            arguments=[a.model_dump() for a in args],
        )
        row = await self.db.get_template(tpl.name)
        assert row is not None
        self._register(row)
        return row

    async def delete(self, name: str) -> None:
        await self.db.delete_template(name)
        try:
            self.engine.server.remove_prompt(name)
        except Exception:
            pass

    async def list(self) -> list[dict[str, Any]]:
        return await self.db.list_templates()

    async def get(self, name: str) -> dict[str, Any] | None:
        return await self.db.get_template(name)

    # ── rendering ────────────────────────────────────────────
    def render_row(self, row: dict[str, Any], arguments: dict[str, Any] | None) -> str:
        values: dict[str, Any] = {}
        for arg in row["arguments"]:
            given = (arguments or {}).get(arg["name"])
            if given not in (None, ""):
                values[arg["name"]] = given
            elif arg.get("default") not in (None, ""):
                values[arg["name"]] = arg["default"]
            elif arg.get("required", True):
                raise ValueError(f"Missing required argument {arg['name']!r}.")
        # Optional args without a value stay undefined so `| default(...)` applies.
        try:
            return self.env.from_string(row["body"]).render(**values).strip()
        except TemplateError as exc:
            raise ValueError(f"Could not render template: {exc}") from None

    async def render(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        row = await self.db.get_template(name)
        if row is None:
            raise KeyError(name)
        return self.render_row(row, arguments)

    def _register(self, row: dict[str, Any]) -> None:
        name = row["name"]

        async def render_prompt(**kwargs: Any) -> str:
            current = await self.db.get_template(name)
            if current is None:
                raise ValueError(f"Template {name!r} no longer exists.")
            return self.render_row(current, kwargs)

        try:
            self.engine.server.remove_prompt(name)
        except Exception:
            pass
        self.engine.server.add_prompt(
            Prompt(
                name=name,
                description=row["description"] or name,
                arguments=[
                    PromptArgument(
                        name=a["name"],
                        description=a.get("description") or None,
                        required=bool(a.get("required", True)) and a.get("default") in (None, ""),
                    )
                    for a in row["arguments"]
                ],
                fn=render_prompt,
            )
        )
