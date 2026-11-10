"""Envelope encryption for secrets stored in the database (SSO client secrets, SMTP passwords,
webhook secrets, provider API keys). Values are encrypted with a key derived from
``TESSERA_MASTER_KEY``; rotating the master key requires re-encrypting stored values."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from tessera.config import get_settings
from tessera.exceptions import ConfigurationError

PREFIX = "enc:v1:"


def _fernet() -> Fernet:
    master = get_settings().master_key_bytes
    if len(master) < 32:
        raise ConfigurationError("TESSERA_MASTER_KEY must decode to at least 32 bytes")
    derived = hashlib.sha256(b"tessera-secrets-v1" + master).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt(value: str) -> str:
    return PREFIX + _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    if not token.startswith(PREFIX):
        return token  # legacy/plain value
    try:
        return _fernet().decrypt(token[len(PREFIX) :].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ConfigurationError("Stored secret cannot be decrypted with the current master key") from exc


def is_encrypted(value: str | None) -> bool:
    return bool(value) and str(value).startswith(PREFIX)


def hash_token(token: str) -> str:
    """Stable hash for session/API tokens (high-entropy inputs, so a plain SHA-256 is adequate)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def sign_payload(secret: str, payload: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
