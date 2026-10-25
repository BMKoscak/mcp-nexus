"""Symmetric encryption for secrets entered in the UI (API keys, webhook URLs).

The key comes from ``NEXUS_SECRET_KEY`` or, if unset, ``<data_dir>/secret.key``
(generated on first use). Losing the key file only means re-entering the keys.
"""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from nexus.config import Settings


class SecretBox:
    def __init__(self, key: bytes) -> None:
        self._fernet = Fernet(key)

    @classmethod
    def from_settings(cls, settings: Settings) -> SecretBox:
        if settings.secret_key is not None:
            return cls(settings.secret_key.get_secret_value().encode())
        return cls(_load_or_create_key(settings.data_dir / "secret.key"))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        """Return the plaintext, or ``None`` if the token was made with another key."""
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except InvalidToken:
            return None


def mask(secret: str | None, visible: int = 4) -> str:
    """``sk-ant-abc…wxyz`` style masking for display. Never returns the full value."""
    if not secret:
        return ""
    if len(secret) <= visible * 2:
        return "•" * len(secret)
    return f"{secret[: min(6, visible + 2)]}…{secret[-visible:]}"


def _load_or_create_key(path: Path) -> bytes:
    if path.exists():
        return path.read_bytes().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows ignores POSIX modes
        pass
    return key
