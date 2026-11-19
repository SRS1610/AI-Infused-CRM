"""Discuss API: my channels with unread counts, browse/join, direct messages, mark read, presence."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from modules.discuss.models.channel import unread_counts
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError
from tessera.i18n import _
from tessera.realtime import hub

router = register_router(APIRouter(prefix="/discuss", tags=["discuss"]))


def _channel_payload(ch: Any, unread: dict[int, int], uid: int) -> dict[str, Any]:
    members = ch.elevated().member_ids
    me = members.filtered(lambda m: m.user_id.ids == [uid])
    others = [m.user_id for m in members if m.user_id.ids != [uid]]
    name = ch.name
    if ch.channel_type == "dm" and others:
        name = others[0].name
    return {
        "id": ch.ids[0],
        "name": name,
        "description": ch.description,
        "type": ch.channel_type,
        "privacy": ch.privacy,
        "member": bool(me),
        "member_count": len(members),
        "notify": me.notify if me else None,
        "unread": unread.get(ch.ids[0], 0),
        "last_message_at": ch.last_message_at.isoformat() if ch.last_message_at else None,
        "partner_user_id": others[0].ids[0] if ch.channel_type == "dm" and others else None,
    }


@router.get("/channels")
def my_channels(ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    member_rows = ctx.env["discuss.member"].elevated().search([("user_id", "=", ctx.uid), ("is_pinned", "=", True)])
    channels = ctx.env["discuss.channel"].browse([m.channel_id.ids[0] for m in member_rows]).exists()
    unread = unread_counts(ctx.env, ctx.uid, list(channels.ids))
    out = [_channel_payload(ch, unread, ctx.uid) for ch in channels]
    return sorted(out, key=lambda c: (c["type"] != "channel", c["name"].lower()))


@router.get("/channels/browse")
def browse(q: str = Query("", max_length=80), ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    """Channels the caller can see (public, their groups, or invited) — record rules decide."""
    domain: list[Any] = [("channel_type", "=", "channel")]
    if q:
        domain.append(("name", "ilike", q))
    channels = ctx.env["discuss.channel"].search(domain, limit=50)
    return [_channel_payload(ch, {}, ctx.uid) for ch in channels]


@router.post("/channels", status_code=201)
def create_channel(
    name: str = Body(..., embed=True, max_length=80),
    description: str | None = Body(None, embed=True),
    privacy: str = Body("public", embed=True, pattern="^(public|invite)$"),
    ctx: RequestContext = Depends(authenticated),
) -> dict[str, Any]:
    ch = ctx.env["discuss.channel"].create({"name": name, "description": description, "privacy": privacy})
    return _channel_payload(ch, {}, ctx.uid)


@router.post("/dm/{user_id}")
def direct_message(user_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    ch = ctx.env["discuss.channel"].get_or_create_dm(user_id)
    return _channel_payload(ch, unread_counts(ctx.env, ctx.uid, ch.ids), ctx.uid)


def _membership(ctx: RequestContext, channel_id: int) -> Any:
    member = (
        ctx.env["discuss.member"]
        .elevated()
        .search([("channel_id", "=", channel_id), ("user_id", "=", ctx.uid)], limit=1)
    )
    if not member:
        raise MissingError(_("You are not a member of this channel"))
    return member


@router.post("/channels/{channel_id}/seen")
def mark_seen(
    channel_id: int, message_id: int = Body(..., embed=True), ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    member = _membership(ctx, channel_id)
    if message_id > (member.last_seen_message_id or 0):
        member.write({"last_seen_message_id": message_id})
    return {"unread": unread_counts(ctx.env, ctx.uid, [channel_id]).get(channel_id, 0)}


@router.post("/channels/{channel_id}/notify")
def set_notify(
    channel_id: int,
    notify: str = Body(..., embed=True, pattern="^(all|mentions|none)$"),
    ctx: RequestContext = Depends(authenticated),
) -> dict[str, Any]:
    _membership(ctx, channel_id).write({"notify": notify})
    return {"notify": notify}


@router.post("/channels/{channel_id}/join")
def join(channel_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    ch = ctx.env["discuss.channel"].browse([channel_id])
    ch.action_join()
    return _channel_payload(ch, {}, ctx.uid)


@router.post("/channels/{channel_id}/leave")
def leave(channel_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    ctx.env["discuss.channel"].browse([channel_id]).action_leave()
    return {"left": True}


@router.post("/channels/{channel_id}/invite")
def invite(
    channel_id: int, user_ids: list[int] = Body(..., embed=True), ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    ch = ctx.env["discuss.channel"].browse([channel_id])
    ch.action_invite(user_ids)
    return _channel_payload(ch, {}, ctx.uid)


@router.get("/presence")
def presence(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Users with an open connection: in every API process with the Redis realtime backend (presence kept
    in Redis with a heartbeat), in this process with the in-process backend."""
    return {"online": sorted(hub.online_uids(ctx.registry.dbname))}
