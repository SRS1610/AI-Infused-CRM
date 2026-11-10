"""Connection registry and fan-out.

``publish()`` may be called from any thread (ORM work runs in FastAPI's threadpool); delivery
to a connection is scheduled on the event loop that owns its WebSocket. With
``TESSERA_REALTIME_BACKEND=redis`` messages go through Redis pub/sub so every process receives
them; the in-process backend is used for development and tests.

Presence (who has at least one open connection): with the in-process backend, the connections of this
process. With the Redis backend, every process records its connections in one sorted set per database
(``tessera:presence:<db>``, member ``<uid>:<connection id>``, score = expiry time) and refreshes them every
``PRESENCE_TTL / 3`` seconds, so any process answers for all of them, and the connections of a process that
stopped disappear after :data:`PRESENCE_TTL` seconds. A user goes online with their first connection in any
process and offline with their last.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tessera.config import get_settings
from tessera.logging import get_logger

if TYPE_CHECKING:
    from tessera.db.engine import Cursor

log = get_logger(__name__)

REDIS_CHANNEL = "tessera:realtime"
MAX_QUEUE = 1000
PRESENCE_KEY = "tessera:presence:{db}"
PRESENCE_TTL = 60  # seconds a connection stays listed without a heartbeat from its process


@dataclass
class Connection:
    id: str
    uid: int
    dbname: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]]
    channels: set[str] = field(default_factory=set)


class Hub:
    def __init__(self) -> None:
        self._connections: dict[str, Connection] = {}
        self._lock = threading.Lock()
        self._redis: Any = None
        self._listener: asyncio.Task[None] | None = None
        self._heartbeat: asyncio.Task[None] | None = None
        self._presence_dbs: set[str] = set()  # databases this process had connections to at the last beat

    # -- connections ------------------------------------------------------------------------
    def connect(self, uid: int, dbname: str) -> Connection:
        conn = Connection(
            id=uuid.uuid4().hex, uid=uid, dbname=dbname, loop=asyncio.get_running_loop(), queue=asyncio.Queue(MAX_QUEUE)
        )
        with self._lock:
            first = not any(c.uid == uid and c.dbname == dbname for c in self._connections.values())
            self._connections[conn.id] = conn
        if self._shared_presence():
            shared = self._presence_update(conn, joined=True)
            first = shared if shared is not None else first
        if first:
            self._announce(dbname, uid, True)
        return conn

    def disconnect(self, conn: Connection) -> None:
        with self._lock:
            known = self._connections.pop(conn.id, None) is not None
            last = not any(c.uid == conn.uid and c.dbname == conn.dbname for c in self._connections.values())
        if not known:
            return  # already gone (a closed event loop and the WebSocket handler can both report it)
        if self._shared_presence():
            shared = self._presence_update(conn, joined=False)
            last = shared if shared is not None else last
        if last:
            self._announce(conn.dbname, conn.uid, False)

    # -- presence ------------------------------------------------------------------------------
    def online_uids(self, dbname: str) -> set[int]:
        """Users with at least one open connection: in any process with the Redis backend, else in this one."""
        if self._shared_presence():
            try:
                members = self._redis_client().zrangebyscore(PRESENCE_KEY.format(db=dbname), time.time(), "+inf")
                return {uid for m in members if (uid := _member_uid(m)) is not None}
            except Exception:
                log.warning("realtime redis presence unavailable; answering for this process only")
        return self.local_online_uids(dbname)

    def local_online_uids(self, dbname: str) -> set[int]:
        with self._lock:
            return {c.uid for c in self._connections.values() if c.dbname == dbname}

    @staticmethod
    def _shared_presence() -> bool:
        return get_settings().realtime_backend == "redis"

    def _presence_update(self, conn: Connection, *, joined: bool) -> bool | None:
        """Record (or drop) ``conn`` in Redis. Returns whether it was the user's first (joined) or last
        (left) live connection across processes, or None when Redis cannot be reached."""
        key = PRESENCE_KEY.format(db=conn.dbname)
        member = f"{conn.uid}:{conn.id}"
        now = time.time()
        try:
            pipe = self._redis_client().pipeline(transaction=True)  # MULTI/EXEC: both steps see one state
            if joined:
                pipe.zadd(key, {member: now + PRESENCE_TTL})
            else:
                pipe.zrem(key, member)
            pipe.zrangebyscore(key, now, "+inf")
            pipe.expire(key, PRESENCE_TTL * 2)
            live = pipe.execute()[1]
        except Exception:
            log.warning("realtime redis presence update failed", uid=conn.uid)
            return None
        return not [m for m in live if _member_uid(m) == conn.uid and _decode(m) != member]

    def presence_heartbeat(self) -> int:
        """Redis backend: extend this process's connections and drop those of processes that stopped
        refreshing theirs, announcing the users who went offline with them. Returns the connections refreshed."""
        if not self._shared_presence():
            return 0
        with self._lock:
            by_db: dict[str, list[Connection]] = {}
            for c in self._connections.values():
                by_db.setdefault(c.dbname, []).append(c)
        now = time.time()
        refreshed = 0
        for dbname in sorted({*by_db, *self._presence_dbs}):
            conns = by_db.get(dbname, [])
            key = PRESENCE_KEY.format(db=dbname)
            try:
                pipe = self._redis_client().pipeline(transaction=True)
                if conns:
                    pipe.zadd(key, {f"{c.uid}:{c.id}": now + PRESENCE_TTL for c in conns})
                pipe.zrangebyscore(key, "-inf", now)
                pipe.zremrangebyscore(key, "-inf", now)
                pipe.zrangebyscore(key, now, "+inf")
                pipe.expire(key, PRESENCE_TTL * 2)
                results = pipe.execute()
            except Exception:
                log.warning("realtime redis presence heartbeat failed")
                continue
            offset = 1 if conns else 0
            expired, live = results[offset], results[offset + 2]
            refreshed += len(conns)
            gone = {_member_uid(m) for m in expired} - {_member_uid(m) for m in live}
            for uid in sorted(u for u in gone if u is not None):
                self._announce(dbname, uid, False)
        self._presence_dbs = set(by_db)
        return refreshed

    def _announce(self, dbname: str, uid: int, online: bool) -> None:
        try:
            self.publish(
                dbname, "presence", {"channel": "presence", "event": "presence", "data": {"uid": uid, "online": online}}
            )
        except Exception:
            log.debug("presence announce failed", uid=uid)

    def subscribe(self, conn: Connection, channels: list[str]) -> None:
        with self._lock:
            conn.channels.update(channels)

    def unsubscribe(self, conn: Connection, channels: list[str]) -> None:
        with self._lock:
            conn.channels.difference_update(channels)

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    # -- delivery ---------------------------------------------------------------------------
    def deliver_local(self, dbname: str, channel: str, message: dict[str, Any]) -> int:
        with self._lock:
            targets = [c for c in self._connections.values() if c.dbname == dbname and channel in c.channels]
        envelope = {"channel": channel, **message}
        for conn in targets:
            try:
                conn.loop.call_soon_threadsafe(_put_nowait, conn.queue, envelope)
            except RuntimeError:  # loop closed: connection is going away
                self.disconnect(conn)
        return len(targets)

    def publish(self, dbname: str, channel: str, message: dict[str, Any]) -> None:
        if get_settings().realtime_backend == "redis":
            try:
                self._redis_client().publish(
                    REDIS_CHANNEL, json.dumps({"db": dbname, "channel": channel, "message": message}, default=str)
                )
                return
            except Exception:
                log.warning("realtime redis publish failed; delivering locally")
        self.deliver_local(dbname, channel, message)

    def _redis_client(self) -> Any:
        if self._redis is None:
            import redis

            self._redis = redis.Redis.from_url(get_settings().redis_url)
        return self._redis

    async def start_listener(self) -> None:
        """Redis backend: forward bus messages to local connections and keep this process's presence fresh
        (one task each per process)."""
        if get_settings().realtime_backend != "redis" or self._listener is not None:
            return

        async def listen() -> None:
            import redis.asyncio as aioredis

            client = aioredis.Redis.from_url(get_settings().redis_url)
            pubsub = client.pubsub()
            await pubsub.subscribe(REDIS_CHANNEL)
            async for item in pubsub.listen():
                if item.get("type") != "message":
                    continue
                try:
                    data = json.loads(item["data"])
                    self.deliver_local(data["db"], data["channel"], data["message"])
                except Exception:
                    log.exception("bad realtime message")

        async def beat() -> None:
            while True:
                await asyncio.sleep(PRESENCE_TTL / 3)
                try:
                    await asyncio.to_thread(self.presence_heartbeat)
                except Exception:
                    log.exception("presence heartbeat failed")

        self._listener = asyncio.create_task(listen())
        self._heartbeat = asyncio.create_task(beat())


def _decode(member: Any) -> str:
    return member.decode() if isinstance(member, bytes) else str(member)


def _member_uid(member: Any) -> int | None:
    head = _decode(member).split(":", 1)[0]
    return int(head) if head.isdigit() else None


def _put_nowait(queue: asyncio.Queue[dict[str, Any]], item: dict[str, Any]) -> None:
    if queue.full():
        with contextlib.suppress(asyncio.QueueEmpty):
            queue.get_nowait()  # drop the oldest message for slow consumers
    queue.put_nowait(item)


hub = Hub()


def publish(dbname: str, channel: str, event: str, data: dict[str, Any] | None = None) -> None:
    hub.publish(dbname, channel, {"event": event, "data": data or {}})


def publish_after_commit(cr: Cursor, channel: str, event: str, data: dict[str, Any] | None = None) -> None:
    """Publish once the transaction commits (nothing is sent for rolled-back work)."""
    dbname = cr.dbname
    cr.after_commit(lambda: publish(dbname, channel, event, data))
