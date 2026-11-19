"""A minimal in-memory Redis for presence tests: the sorted-set, expiry and publish commands the hub
uses, with pipelines executed atomically like MULTI/EXEC. Members come back as bytes, as from redis-py."""

from __future__ import annotations

import threading
from typing import Any


def _bound(value: Any) -> float:
    if value in ("-inf", b"-inf"):
        return float("-inf")
    if value in ("+inf", "inf", b"+inf"):
        return float("inf")
    return float(value)


class MemoryRedis:
    def __init__(self) -> None:
        self.zsets: dict[str, dict[str, float]] = {}
        self.expiries: dict[str, int] = {}
        self.published: list[tuple[str, str]] = []
        self.lock = threading.RLock()

    def zadd(self, key: str, mapping: dict[str, float]) -> int:
        with self.lock:
            zset = self.zsets.setdefault(key, {})
            new = sum(1 for m in mapping if m not in zset)
            zset.update({str(m): float(s) for m, s in mapping.items()})
            return new

    def zrem(self, key: str, *members: str) -> int:
        with self.lock:
            zset = self.zsets.get(key, {})
            return sum(1 for m in members if zset.pop(str(m), None) is not None)

    def zrangebyscore(self, key: str, low: Any, high: Any) -> list[bytes]:
        lo, hi = _bound(low), _bound(high)
        with self.lock:
            items = sorted(self.zsets.get(key, {}).items(), key=lambda kv: (kv[1], kv[0]))
            return [m.encode() for m, s in items if lo <= s <= hi]

    def zremrangebyscore(self, key: str, low: Any, high: Any) -> int:
        lo, hi = _bound(low), _bound(high)
        with self.lock:
            zset = self.zsets.get(key, {})
            gone = [m for m, s in zset.items() if lo <= s <= hi]
            for m in gone:
                del zset[m]
            return len(gone)

    def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = int(seconds)
        return key in self.zsets

    def publish(self, channel: str, message: str) -> int:
        self.published.append((channel, message))
        return 0

    def pipeline(self, transaction: bool = True) -> Pipeline:
        return Pipeline(self)


class Pipeline:
    def __init__(self, redis: MemoryRedis) -> None:
        self.redis = redis
        self.queued: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Any:
        def queue(*args: Any) -> Pipeline:
            self.queued.append((name, args))
            return self

        return queue

    def execute(self) -> list[Any]:
        with self.redis.lock:
            return [getattr(self.redis, name)(*args) for name, args in self.queued]
