"""LLM Hub backend: provider credentials, model discovery, connection tests, routing.

Models are addressed as ``<provider>/<model>`` (``anthropic/claude-sonnet-5-5``,
``ollama/llama3.2:latest``). Credentials resolve environment → encrypted SQLite.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from nexus.config import Settings
from nexus.crypto import SecretBox, mask
from nexus.db import Database
from nexus.providers import PROVIDER_CLASSES, Message, Provider, ProviderError, TextDelta

ACTIVE_MODEL_KEY = "active_model"
LLM_TIMEOUT = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)
LIST_TIMEOUT = 8.0


class LLMRouter:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        crypto: SecretBox,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.db = db
        self.crypto = crypto
        self._transport = http_transport
        self.http: httpx.AsyncClient | None = None
        self.classes: dict[str, type[Provider]] = dict(PROVIDER_CLASSES)

    async def start(self) -> None:
        self.http = httpx.AsyncClient(timeout=LLM_TIMEOUT, transport=self._transport)

    async def stop(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None

    def register(self, name: str, cls: type[Provider]) -> None:
        """Add a provider class (used by tests and future plugins)."""
        self.classes[name] = cls

    # ── credentials ──────────────────────────────────────────
    def _env_key(self, name: str) -> str | None:
        secret = {
            "anthropic": self.settings.anthropic_api_key,
            "openai": self.settings.openai_api_key,
            "gemini": self.settings.gemini_api_key,
        }.get(name)
        return secret.get_secret_value() if secret else None

    def _env_base_url(self, name: str) -> str | None:
        if name == "ollama" and "ollama_base_url" in self.settings.model_fields_set:
            return self.settings.ollama_base_url
        return None

    async def _resolve(self, name: str) -> tuple[str | None, str | None, str, str | None]:
        """→ (api_key, base_url, key_source, base_url_source)."""
        row = await self.db.get_provider(name) or {}
        key, key_source = self._env_key(name), "env"
        if not key and row.get("api_key_enc"):
            key, key_source = self.crypto.decrypt(row["api_key_enc"]), "ui"
        if not key:
            key_source = "none"
        base = self._env_base_url(name)
        base_source = "env" if base else None
        if not base and row.get("base_url"):
            base, base_source = row["base_url"], "ui"
        if not base and name == "ollama":
            base, base_source = self.settings.ollama_base_url, "default"
        return key, base, key_source, base_source

    async def provider(self, name: str) -> Provider:
        if name not in self.classes:
            raise ProviderError(f"Unknown provider {name!r}. Known: {', '.join(self.classes)}.")
        if self.http is None:
            raise RuntimeError("LLMRouter is not started")
        key, base, _, _ = await self._resolve(name)
        return self.classes[name](self.http, api_key=key, base_url=base)

    async def list_providers(self) -> list[dict[str, Any]]:
        out = []
        for name, cls in self.classes.items():
            row = await self.db.get_provider(name) or {}
            key, base, key_source, base_source = await self._resolve(name)
            out.append(
                {
                    "name": name,
                    "label": cls.label or name,
                    "needs_api_key": cls.needs_api_key,
                    "configured": bool(key) or not cls.needs_api_key,
                    "api_key_masked": mask(key),
                    "api_key_source": key_source,
                    "base_url": base or cls.default_base_url,
                    "base_url_source": base_source or "default",
                    "enabled": row.get("enabled", True),
                }
            )
        return out

    async def configure(
        self,
        name: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        enabled: bool | None = None,
        clear_key: bool = False,
    ) -> None:
        if name not in self.classes:
            raise ProviderError(f"Unknown provider {name!r}.")
        row = await self.db.get_provider(name) or {}
        await self.db.upsert_provider(
            name,
            base_url=(base_url.strip() or None) if base_url is not None else row.get("base_url"),
            api_key_enc=self.crypto.encrypt(api_key.strip()) if api_key else None,
            enabled=row.get("enabled", True) if enabled is None else enabled,
        )
        if clear_key:
            await self.db.clear_provider_key(name)

    # ── models ───────────────────────────────────────────────
    async def list_models(self, name: str | None = None) -> dict[str, Any]:
        names = [name] if name else list(self.classes)

        async def one(n: str) -> tuple[str, dict[str, Any]]:
            row = await self.db.get_provider(n) or {}
            if not row.get("enabled", True):
                return n, {"models": [], "error": "disabled"}
            try:
                p = await self.provider(n)
                if p.needs_api_key and not p.api_key:
                    return n, {"models": [], "error": "no API key"}
                models = await asyncio.wait_for(p.list_models(), LIST_TIMEOUT)
                return n, {
                    "models": [{"id": f"{n}/{m.id}", "name": m.name or m.id} for m in models]
                }
            except (ProviderError, httpx.HTTPError, TimeoutError) as exc:
                return n, {"models": [], "error": _short(exc)}

        results = await asyncio.gather(*(one(n) for n in names))
        return dict(results)

    async def test_connection(self, name: str, model: str | None = None) -> dict[str, Any]:
        """List models, then (if a model is given) stream a tiny completion."""
        t0 = time.perf_counter()
        try:
            p = await self.provider(name)
            models = await asyncio.wait_for(p.list_models(), LIST_TIMEOUT)
            result: dict[str, Any] = {"ok": True, "models": len(models)}
            if model:
                text = ""
                async for ev in p.stream(
                    model, [Message("user", "Reply with the single word: pong")], max_tokens=16
                ):
                    if isinstance(ev, TextDelta):
                        text += ev.text
                result["reply"] = text.strip()[:100]
        except (ProviderError, httpx.HTTPError, TimeoutError) as exc:
            result = {"ok": False, "error": _short(exc)}
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000)
        return result

    # ── routing ──────────────────────────────────────────────
    @staticmethod
    def split(model_ref: str) -> tuple[str, str]:
        provider, sep, model = model_ref.partition("/")
        if not sep or not model:
            raise ProviderError(f"Model must look like 'provider/model', got {model_ref!r}.")
        return provider, model

    async def resolve_model(self, model_ref: str | None) -> tuple[Provider, str, str]:
        ref = model_ref or await self.active_model()
        provider_name, model = self.split(ref)
        return await self.provider(provider_name), model, ref

    async def active_model(self) -> str:
        return await self.db.get_setting(ACTIVE_MODEL_KEY, self.settings.default_model)

    async def set_active_model(self, model_ref: str) -> None:
        self.split(model_ref)
        await self.db.set_setting(ACTIVE_MODEL_KEY, model_ref)


def _short(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, httpx.ConnectError):
        return "connection refused (is the server running?)"
    text = str(exc) or exc.__class__.__name__
    return text[:300]
