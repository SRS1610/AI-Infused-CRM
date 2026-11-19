"""Presence across API processes (Redis backend) and within one process (in-process backend).

Each ``Hub`` instance stands for one API process; with the Redis backend they share one Redis (the
in-memory stand-in, or a real ``redis-server`` when one is installed)."""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from typing import Any

import pytest

import tessera.realtime.hub  # noqa: F401 - loads the module read from sys.modules below
from tessera.config import get_settings
from tessera.realtime.tests.memory_redis import MemoryRedis

H = sys.modules["tessera.realtime.hub"]  # the module (the package exports the ``hub`` instance under that name)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(H, "time", c)
    return c


@pytest.fixture
def redis_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "realtime_backend", "redis")


def processes(redis: Any, n: int = 2) -> list[H.Hub]:
    hubs = [H.Hub() for _ in range(n)]
    for h in hubs:
        h._redis = redis
    return hubs


def announcements(redis: MemoryRedis) -> list[tuple[int, bool]]:
    out = []
    for _, raw in redis.published:
        msg = json.loads(raw)
        if msg["channel"] == "presence":
            out.append((msg["message"]["data"]["uid"], msg["message"]["data"]["online"]))
    return out


def test_any_process_answers_for_every_process(redis_backend: None, clock: Clock) -> None:
    redis = MemoryRedis()
    a, b = processes(redis)

    async def scenario() -> None:
        a7 = a.connect(7, "db")
        assert b.online_uids("db") == {7} and b.local_online_uids("db") == set()
        b7 = b.connect(7, "db")  # second tab, served by the other process: already online
        b8 = b.connect(8, "db")
        assert a.online_uids("db") == b.online_uids("db") == {7, 8}
        assert a.online_uids("other_db") == set()
        a.disconnect(a7)
        assert b.online_uids("db") == {7, 8}  # still connected through the other process
        b.disconnect(b7)
        b.disconnect(b7)  # reported twice: no second announcement
        assert a.online_uids("db") == {8}
        b.disconnect(b8)
        assert a.online_uids("db") == set()

    asyncio.run(scenario())
    assert announcements(redis) == [(7, True), (8, True), (7, False), (8, False)]
    assert redis.expiries == {"tessera:presence:db": H.PRESENCE_TTL * 2}


def test_connections_of_a_stopped_process_expire(redis_backend: None, clock: Clock) -> None:
    redis = MemoryRedis()
    a, b = processes(redis)

    async def scenario() -> None:
        a.connect(9, "db")
        b.connect(5, "db")
        for _ in range(4):  # both processes alive: their heartbeats keep everyone listed
            clock.now += H.PRESENCE_TTL / 2
            assert a.presence_heartbeat() == 1 and b.presence_heartbeat() == 1
        assert b.online_uids("db") == {5, 9}
        redis.published.clear()
        clock.now += H.PRESENCE_TTL + 1  # process a stops without closing its connections
        assert b.online_uids("db") == set()  # past their expiry, nobody is listed …
        b.presence_heartbeat()  # … and the next beat of a live process drops them
        assert b.online_uids("db") == {5}
        assert redis.zsets["tessera:presence:db"].keys() == {f"5:{c.id}" for c in b._connections.values()}

    asyncio.run(scenario())
    assert announcements(redis) == [(9, False)]


def test_unreachable_redis_falls_back_to_this_process(redis_backend: None, clock: Clock) -> None:
    class Down:
        def __getattr__(self, name: str) -> Any:
            raise ConnectionError("redis is down")

    (a,) = processes(Down(), 1)

    async def scenario() -> None:
        conn = a.connect(3, "db")
        assert a.online_uids("db") == {3}
        assert a.presence_heartbeat() == 0
        a.disconnect(conn)
        assert a.online_uids("db") == set()

    asyncio.run(scenario())


def test_in_process_backend_counts_this_process_only(clock: Clock) -> None:
    assert get_settings().realtime_backend == "memory"
    redis = MemoryRedis()
    a, b = processes(redis)

    async def scenario() -> None:
        conn = a.connect(4, "db")
        assert a.online_uids("db") == {4} and b.online_uids("db") == set()
        assert a.presence_heartbeat() == 0
        a.disconnect(conn)

    asyncio.run(scenario())
    assert redis.zsets == {} and redis.published == []


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
def real_redis_url() -> Iterator[str]:
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server is not installed")
    port = _free_port()
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import redis

        client = redis.Redis(host="127.0.0.1", port=port)
        for _ in range(50):
            try:
                client.ping()
                break
            except redis.ConnectionError:
                time.sleep(0.1)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        proc.terminate()
        proc.wait(5)


def test_presence_against_a_real_redis(
    redis_backend: None, real_redis_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import redis

    monkeypatch.setattr(get_settings(), "redis_url", real_redis_url)
    a, b = H.Hub(), H.Hub()  # each opens its own connection to Redis, like two API processes

    client = redis.Redis.from_url(real_redis_url)

    async def scenario() -> None:
        a1 = a.connect(11, "realdb")
        b1 = b.connect(12, "realdb")
        assert a.online_uids("realdb") == b.online_uids("realdb") == {11, 12}
        assert a.presence_heartbeat() == 1
        assert 0 < client.ttl("tessera:presence:realdb") <= H.PRESENCE_TTL * 2  # the key expires if all stop
        a.disconnect(a1)
        assert b.online_uids("realdb") == {12}
        b.disconnect(b1)
        assert a.online_uids("realdb") == set()

    asyncio.run(scenario())
