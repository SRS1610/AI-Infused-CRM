"""Response cache keyed by (company, prompt version, model, hash of the redacted request, schema).

Only features that declare a TTL are cached (summaries of unchanged threads); drafts never are.
The runtime puts the source records' ``write_date`` in ``metadata.cache_key_extra`` so an edit
produces a new key."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Protocol


def cache_key(company_id: int, prompt_version: str, model: str, payload: dict[str, Any]) -> str:
    body = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(body.encode()).hexdigest()
    return f"gw:resp:{company_id}:{prompt_version}:{model}:{digest}"


class ResponseCache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, ttl: int) -> None: ...


class MemoryCache:
    def __init__(self, max_entries: int = 2048) -> None:
        self.max_entries = max_entries
        self._data: dict[str, tuple[float, str]] = {}

    async def get(self, key: str) -> str | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        expires, value = hit
        if time.monotonic() > expires:
            self._data.pop(key, None)
            return None
        return value

    async def set(self, key: str, value: str, ttl: int) -> None:
        if len(self._data) >= self.max_entries:
            self._data.pop(next(iter(self._data)))
        self._data[key] = (time.monotonic() + ttl, value)


class RedisCache:  # pragma: no cover - exercised in the compose stack
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self.client = redis.from_url(url)

    async def get(self, key: str) -> str | None:
        value = await self.client.get(key)
        return value.decode() if isinstance(value, bytes) else value

    async def set(self, key: str, value: str, ttl: int) -> None:
        await self.client.set(key, value, ex=ttl)
