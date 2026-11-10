"""WebSocket endpoint of the realtime hub (``/api/v1/ws``).

Protocol (JSON text frames)::

    → {"op": "subscribe", "channels": ["record:crm.lead:42", "user:7"]}
    ← {"op": "subscribed", "channels": [...], "denied": [...]}
    ← {"channel": "record:crm.lead:42", "event": "write", "data": {"fields": ["stage_id"], "uid": 3}}
    → {"op": "unsubscribe", "channels": [...]}
    → {"op": "ping"}  ←  {"op": "pong"}
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from tessera.config import get_settings
from tessera.db.engine import Cursor, get_engine
from tessera.exceptions import AccessError, MissingError
from tessera.logging import get_logger
from tessera.orm.environment import SYSTEM_UID, Environment
from tessera.orm.registry import Registry
from tessera.realtime.hub import hub
from tessera.security.auth import API_KEY_PREFIX, resolve_api_key, resolve_session

log = get_logger(__name__)
router = APIRouter(tags=["realtime"])

MAX_CHANNELS = 200


def _authenticate(cookie_token: str | None, api_key: str | None) -> tuple[int, str] | None:
    url = get_settings().database_url
    registry = Registry.get(url)
    cr = Cursor(get_engine(url))
    try:
        env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        uid: int | None = None
        if api_key and api_key.startswith(API_KEY_PREFIX):
            key = resolve_api_key(env, api_key)
            uid = key.user_id.ids[0] if key is not None else None
        elif cookie_token:
            session = resolve_session(env, cookie_token)
            uid = session.user_id.ids[0] if session is not None else None
        cr.commit()
        return (uid, registry.dbname) if uid else None
    finally:
        cr.close()


def authorize_channels(uid: int, channels: list[str]) -> tuple[list[str], list[str]]:
    """Return (allowed, denied). Runs ORM access checks as ``uid``."""
    url = get_settings().database_url
    registry = Registry.get(url)
    cr = Cursor(get_engine(url))
    allowed: list[str] = []
    denied: list[str] = []
    try:
        sys_env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        user = sys_env["core.user"].browse([uid])
        ctx = {"allowed_company_ids": user.company_ids.ids or user.company_id.ids}
        env = Environment(sys_env.transaction, uid, ctx)
        for channel in channels[:MAX_CHANNELS]:
            kind, _, rest = channel.partition(":")
            ok = False
            try:
                if kind == "user":
                    ok = rest == str(uid)
                elif kind == "model" and rest in registry.models:
                    ok = env[rest].check_access_rights("read", raise_exception=False)
                elif kind == "record":
                    model_name, _, rid = rest.rpartition(":")
                    if model_name in registry.models and rid.isdigit():
                        env[model_name].browse([int(rid)]).check_access("read")
                        ok = True
                elif kind == "job" and rest.isdigit() and "core.job" in registry.models:
                    env["core.job"].browse([int(rest)]).check_access("read")
                    ok = True
                elif kind == "ai":
                    ok = len(rest) >= 32  # AI run channels are random uuids handed only to the run's owner
            except (AccessError, MissingError):
                ok = False
            (allowed if ok else denied).append(channel)
        return allowed, denied
    finally:
        cr.rollback()
        cr.close()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    token = ws.cookies.get(get_settings().session_cookie_name)
    api_key = ws.query_params.get("api_key")
    identity = await run_in_threadpool(_authenticate, token, api_key)
    if identity is None:
        await ws.close(code=4401)
        return
    uid, dbname = identity
    await ws.accept()
    conn = hub.connect(uid, dbname)
    hub.subscribe(conn, [f"user:{uid}"])

    async def sender() -> None:
        while True:
            message = await conn.queue.get()
            await ws.send_text(json.dumps(message, default=str))

    send_task = asyncio.create_task(sender())
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                await conn.queue.put({"op": "error", "message": "invalid JSON"})
                continue
            op = msg.get("op")
            channels = [str(c) for c in msg.get("channels", []) if isinstance(c, str)]
            if op == "subscribe":
                allowed, denied = await run_in_threadpool(authorize_channels, uid, channels)
                hub.subscribe(conn, allowed)
                await conn.queue.put({"op": "subscribed", "channels": allowed, "denied": denied})
            elif op == "unsubscribe":
                hub.unsubscribe(conn, channels)
                await conn.queue.put({"op": "unsubscribed", "channels": channels})
            elif op == "ping":
                # replies share the event queue so they are ordered after already-published events
                await conn.queue.put({"op": "pong"})
            else:
                await conn.queue.put({"op": "error", "message": f"unknown op {op!r}"})
    except WebSocketDisconnect:
        pass
    finally:
        send_task.cancel()
        hub.disconnect(conn)
