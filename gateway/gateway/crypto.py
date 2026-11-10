"""Decrypts provider API keys stored by the application (same Fernet envelope as ``tessera.security.crypto``)."""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"


def _fernet(master_key: str) -> Fernet:
    master = base64.b64decode(master_key)
    if len(master) < 32:
        raise ValueError("master key must decode to at least 32 bytes")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(b"tessera-secrets-v1" + master).digest()))


def encrypt(value: str, master_key: str) -> str:
    return PREFIX + _fernet(master_key).encrypt(value.encode()).decode("ascii")


def decrypt(token: str | None, master_key: str) -> str | None:
    if not token:
        return None
    if not token.startswith(PREFIX):
        return token
    try:
        return _fernet(master_key).decrypt(token[len(PREFIX) :].encode("ascii")).decode()
    except InvalidToken:
        return None  # wrong master key: the provider is treated as having no key (calls fail closed)
