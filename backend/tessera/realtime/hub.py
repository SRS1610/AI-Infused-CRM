"""Connection registry and fan-out.

``publish()`` may be called from any thread (ORM work runs in FastAPI's threadpool); delivery
to a connection is scheduled on the event loop that owns its WebSocket. With
``TESSERA_REALTIME_BACKEND=redis`` messages go through Redis pub/sub so every process receives
them; the in-process backend is used for development and tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
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

    # -- connections ------------------------------------------------------------------------
    def connect(self, uid: int, dbname: str) -> Connection:
        conn = Connection(
            id=uuid.uuid4().hex, uid=uid, dbname=dbname, loop=asyncio.get_running_loop(), queue=asyncio.Queue(MAX_QUEUE)
        )
        with self._lock:
            first = not any(c.uid == uid and c.dbname == dbname for c in self._connections.values())
            self._connections[conn.id] = conn
        if first:
            self._announce(dbname, uid, True)
        return conn

    def disconnect(self, conn: Connection) -> None:
        with self._lock:
            self._connections.pop(conn.id, None)
            last = not any(c.uid == conn.uid and c.dbname == conn.dbname for c in self._connections.values())
        if last:
            self._announce(conn.dbname, conn.uid, False)

    # -- presence (users with at least one open connection in this process) ------------------
    def online_uids(self, dbname: str) -> set[int]:
        with self._lock:
            return {c.uid for c in self._connections.values() if c.dbname == dbname}

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
        """Redis backend: forward bus messages to local connections (one task per process)."""
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

        self._listener = asyncio.create_task(listen())


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
