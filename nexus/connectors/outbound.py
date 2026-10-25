"""Outbound dispatcher ("Send Stuff"): webhooks, Discord, Slack, email, push notifications.

Every sending tool is ``requires_approval=True``, so it waits for a decision in
the UI unless auto-approve is on. Named targets resolve server-side: the model
asks for ``target="discord"`` and never sees the webhook URL.
"""

from __future__ import annotations

from email.message import EmailMessage
from typing import Annotated, Any, Literal

import aiosmtplib
import httpx
from pydantic import BaseModel, Field, SecretStr

from nexus.connectors.base import Connector, ToolError, tool

DISCORD_LIMIT = 2000
RESPONSE_PREVIEW = 500


class OutboundConfig(BaseModel):
    allow_raw_urls: bool = Field(
        True,
        description="Allow arbitrary URLs (still approval-gated), not only named targets.",
    )
    ntfy_server: str = Field("https://ntfy.sh", description="ntfy server for push notifications.")
    ntfy_topic: str = Field(
        "", description="ntfy topic to publish to (subscribe to it in the ntfy app)."
    )
    ntfy_token: SecretStr = Field(SecretStr(""), description="Optional ntfy access token.")
    smtp_host: str = Field("", description="Overrides NEXUS_SMTP_HOST when set.")
    smtp_port: int = Field(0, description="Overrides NEXUS_SMTP_PORT when non-zero.")
    smtp_username: str = Field("", description="Overrides NEXUS_SMTP_USERNAME.")
    smtp_password: SecretStr = Field(SecretStr(""), description="Overrides NEXUS_SMTP_PASSWORD.")
    smtp_from: str = Field("", description="Overrides NEXUS_SMTP_FROM.")


class OutboundConnector(Connector):
    name = "outbound"
    description = (
        "Send webhooks, Discord/Slack messages, emails and push notifications (approval-gated)."
    )
    Config = OutboundConfig

    config: OutboundConfig

    # ── target resolution ────────────────────────────────────
    async def _targets(self) -> dict[str, dict[str, str]]:
        """name → {kind, url}. Built-in env targets first, then UI-saved ones."""
        s = self.ctx.settings
        out: dict[str, dict[str, str]] = {}
        if s.discord_webhook_url and s.discord_webhook_url.get_secret_value():
            out["discord"] = {"kind": "discord", "url": s.discord_webhook_url.get_secret_value()}
        if s.slack_webhook_url and s.slack_webhook_url.get_secret_value():
            out["slack"] = {"kind": "slack", "url": s.slack_webhook_url.get_secret_value()}
        for row in await self.ctx.db.list_webhook_targets():
            url = self.ctx.crypto.decrypt(row["url_enc"])
            if url:
                out.setdefault(
                    row["name"],
                    {"kind": row["kind"], "url": url, "description": row["description"]},
                )
        return out

    async def _resolve(self, target: str | None, url: str | None, kind: str | None = None) -> str:
        if target:
            targets = await self._targets()
            if target not in targets:
                known = ", ".join(sorted(targets)) or "none configured"
                raise ToolError(f"Unknown target {target!r}. Known targets: {known}.")
            entry = targets[target]
            if kind and entry["kind"] != kind:
                raise ToolError(f"Target {target!r} is a {entry['kind']} target, not {kind}.")
            return entry["url"]
        if url:
            if not self.config.allow_raw_urls:
                raise ToolError("Raw URLs are disabled; use a named target.")
            if not url.startswith(("http://", "https://")):
                raise ToolError("URL must start with http:// or https://.")
            return url
        raise ToolError("Provide a named target or a url.")

    async def _post(self, url: str, *, method: str = "POST", **kwargs: Any) -> str:
        try:
            resp = await self.ctx.http.request(method, url, timeout=20, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"Request failed: {exc.__class__.__name__}: {exc}") from None
        body = resp.text[:RESPONSE_PREVIEW]
        if resp.status_code >= 400:
            raise ToolError(f"Remote returned HTTP {resp.status_code}: {body}")
        return f"HTTP {resp.status_code}" + (f": {body}" if body.strip() else "")

    # ── tools ────────────────────────────────────────────────
    @tool(read_only=True)
    async def list_targets(self) -> dict[str, Any]:
        """Named destinations you can send to, plus which channels (email, push) are configured."""
        targets = await self._targets()
        return {
            "targets": [
                {"name": n, "kind": t["kind"], "description": t.get("description", "")}
                for n, t in sorted(targets.items())
            ],
            "email_configured": bool(self._smtp()["host"]),
            "push_configured": bool(self.config.ntfy_topic),
            "raw_urls_allowed": self.config.allow_raw_urls,
        }

    @tool(
        requires_approval=True,
        open_world=True,
        summary="HTTP {method} to {target}{url}",
    )
    async def send_webhook(
        self,
        payload: Annotated[
            dict | list | str, Field(description="JSON body (object/array) or raw text")
        ],
        target: Annotated[str | None, Field(description="Named target from list_targets")] = None,
        url: Annotated[str | None, Field(description="Raw URL, if allowed")] = None,
        method: Literal["POST", "PUT", "PATCH"] = "POST",
        headers: dict[str, str] | None = None,
    ) -> str:
        """Send an HTTP request with a JSON or text body to a webhook."""
        dest = await self._resolve(target, url)
        if isinstance(payload, str):
            return await self._post(dest, method=method, content=payload, headers=headers)
        return await self._post(dest, method=method, json=payload, headers=headers)

    @tool(requires_approval=True, open_world=True, summary="Discord ({target}): {content}")
    async def send_discord(
        self,
        content: Annotated[str, Field(description="Message text (max 2000 characters)")],
        target: str = "discord",
        username: str | None = None,
    ) -> str:
        """Post a message to a Discord channel via its webhook."""
        if len(content) > DISCORD_LIMIT:
            raise ToolError(f"Discord messages are limited to {DISCORD_LIMIT} characters.")
        dest = await self._resolve(target, None, kind="discord")
        body: dict[str, Any] = {"content": content}
        if username:
            body["username"] = username
        return await self._post(dest, json=body)

    @tool(requires_approval=True, open_world=True, summary="Slack ({target}): {text}")
    async def send_slack(self, text: str, target: str = "slack") -> str:
        """Post a message to a Slack channel via an incoming webhook."""
        dest = await self._resolve(target, None, kind="slack")
        return await self._post(dest, json={"text": text})

    @tool(requires_approval=True, open_world=True, summary="Email to {to}: {subject}")
    async def send_email(
        self,
        to: Annotated[list[str], Field(description="Recipient addresses")],
        subject: str,
        body: str,
        html: bool = False,
    ) -> str:
        """Send an email through the configured SMTP server."""
        smtp = self._smtp()
        if not smtp["host"] or not smtp["sender"]:
            raise ToolError("Email is not configured (set SMTP host and from address).")
        if not to:
            raise ToolError("At least one recipient is required.")
        msg = EmailMessage()
        msg["From"] = smtp["sender"]
        msg["To"] = ", ".join(to)
        msg["Subject"] = subject
        if html:
            msg.set_content("This message is HTML; view it in an HTML-capable client.")
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)
        try:
            await aiosmtplib.send(
                msg,
                hostname=smtp["host"],
                port=smtp["port"],
                username=smtp["username"] or None,
                password=smtp["password"] or None,
                start_tls=self.ctx.settings.smtp_starttls if smtp["port"] != 465 else False,
                use_tls=smtp["port"] == 465,
                timeout=30,
            )
        except (aiosmtplib.SMTPException, OSError) as exc:
            raise ToolError(f"SMTP error: {exc}") from None
        return f"Email sent to {len(to)} recipient(s)."

    @tool(requires_approval=True, open_world=True, summary="Push notification: {title} — {message}")
    async def notify(
        self,
        message: str,
        title: str = "MCP Nexus",
        priority: Annotated[int, Field(ge=1, le=5, description="1 = min, 5 = urgent")] = 3,
        tags: list[str] | None = None,
    ) -> str:
        """Send a push notification to your phone/desktop via ntfy."""
        if not self.config.ntfy_topic:
            raise ToolError(
                "Push notifications need an ntfy topic in the outbound connector settings."
            )
        headers = {"Title": title, "Priority": str(priority)}
        if tags:
            headers["Tags"] = ",".join(tags)
        token = self.config.ntfy_token.get_secret_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = f"{self.config.ntfy_server.rstrip('/')}/{self.config.ntfy_topic}"
        return await self._post(url, content=message.encode(), headers=headers)

    # ── helpers ──────────────────────────────────────────────
    def _smtp(self) -> dict[str, Any]:
        s, c = self.ctx.settings, self.config
        password = c.smtp_password.get_secret_value() or (
            s.smtp_password.get_secret_value() if s.smtp_password else ""
        )
        return {
            "host": c.smtp_host or s.smtp_host or "",
            "port": c.smtp_port or s.smtp_port,
            "username": c.smtp_username or s.smtp_username or "",
            "password": password,
            "sender": c.smtp_from or s.smtp_from or "",
        }
