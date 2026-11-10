"""Chatter HTTP API. Every thread endpoint checks access to the underlying record first."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import BaseModel, Field

from modules.chatter.thread import (
    check_thread_access,
    is_internal_user,
    post_message,
    schedule_activity,
    subscribe,
)
from tessera.api.deps import RequestContext, admin_only, authenticated, authenticated_any, request_context
from tessera.api.registry import register_router
from tessera.config import get_settings
from tessera.exceptions import AccessDenied, AccessError, MissingError, UserError

router = register_router(APIRouter(prefix="/chatter", tags=["chatter"]))


class PostIn(BaseModel):
    body: str = Field(max_length=200_000)
    subject: str | None = None
    internal: bool = False
    attachment_ids: list[int] = []
    mention_user_ids: list[int] = []
    parent_id: int | None = None


class ActivityIn(BaseModel):
    activity_type: str | int = "chatter.activity_type_todo"
    summary: str | None = None
    date_deadline: str | None = None
    user_id: int | None = None
    note: str | None = None


class ReactionIn(BaseModel):
    emoji: str = Field(min_length=1, max_length=16)


def _user_names(ctx: RequestContext, ids: set[int]) -> dict[int, str]:
    users = ctx.env["core.user"].elevated().browse(sorted(i for i in ids if i))
    return {u.ids[0]: u.name for u in users.exists()}


def serialize_messages(ctx: RequestContext, messages: Any) -> list[dict[str, Any]]:
    messages = messages.elevated()
    authors = {m.author_id.ids[0] for m in messages if m.author_id}
    reactors = {r.user_id.ids[0] for m in messages for r in m.reaction_ids}
    names = _user_names(ctx, authors | reactors)
    out = []
    for m in messages:
        reactions: dict[str, dict[str, Any]] = {}
        for r in m.reaction_ids:
            entry = reactions.setdefault(r.emoji, {"emoji": r.emoji, "count": 0, "users": [], "mine": False})
            entry["count"] += 1
            entry["users"].append(names.get(r.user_id.ids[0], "?"))
            entry["mine"] = entry["mine"] or r.user_id.ids[0] == ctx.uid
        out.append(
            {
                "id": m.ids[0],
                "type": m.message_type,
                "internal": m.is_internal,
                "subject": m.subject,
                "body": m.body or "",
                "date": m.date.isoformat() if m.date else None,
                "author": {"id": m.author_id.ids[0], "name": names.get(m.author_id.ids[0])} if m.author_id else None,
                "email_from": m.email_from,
                "subtype": m.subtype_id.name if m.subtype_id else None,
                "parent_id": m.parent_id.ids[0] if m.parent_id else None,
                "tracking": [
                    {"field": t.field_name, "label": t.field_label, "old": t.old_display, "new": t.new_display}
                    for t in m.tracking_value_ids
                ],
                "attachments": [
                    {"id": a.ids[0], "name": a.name, "mimetype": a.mimetype, "size": a.file_size}
                    for a in m.attachment_ids
                ],
                "reactions": list(reactions.values()),
                "ai_assisted": bool(m.ai_run_id),
                "editable": bool(m.author_id)
                and m.author_id.ids[0] == ctx.uid
                and m.message_type in ("comment", "note"),
            }
        )
    return out


@router.get("/{model}/{res_id}/messages")
def list_messages(
    model: str,
    res_id: int,
    before_id: int | None = None,
    limit: int = Query(30, ge=1, le=200),
    ctx: RequestContext = Depends(authenticated_any),
) -> dict[str, Any]:
    check_thread_access(ctx.env, model, res_id, "read")
    domain: list[Any] = [("model", "=", model), ("res_id", "=", res_id)]
    if before_id:
        domain.append(("id", "<", before_id))
    if not is_internal_user(ctx.env):
        domain.append(("is_internal", "=", False))
    messages = ctx.env["chatter.message"].elevated().search(domain, order="id desc", limit=limit + 1)
    has_more = len(messages) > limit
    return {"items": serialize_messages(ctx, messages[:limit] if has_more else messages), "has_more": has_more}


@router.post("/{model}/{res_id}/messages", status_code=201)
def create_message(
    model: str, res_id: int, data: PostIn, ctx: RequestContext = Depends(authenticated_any)
) -> dict[str, Any]:
    if not data.body.strip() and not data.attachment_ids:
        raise UserError("A message needs a body or an attachment")
    msg = post_message(
        ctx.env,
        model,
        res_id,
        data.body,
        subject=data.subject,
        message_type="note" if data.internal else "comment",
        attachment_ids=data.attachment_ids,
        mention_user_ids=data.mention_user_ids,
        parent_id=data.parent_id,
        check_access=True,
    )
    return serialize_messages(ctx, msg)[0]


def _own_message(ctx: RequestContext, message_id: int) -> Any:
    msg = ctx.env["chatter.message"].elevated().browse([message_id]).exists()
    if not msg:
        raise MissingError("Message not found")
    check_thread_access(ctx.env, msg.model, msg.res_id, "read")
    is_admin = ctx.registry.security.has_groups(ctx.env, ["core.group_system"])
    if not is_admin and (not msg.author_id or msg.author_id.ids[0] != ctx.uid):
        raise AccessError("You can only change your own messages")
    if msg.message_type not in ("comment", "note"):
        raise UserError("Only comments and notes can be changed")
    return msg


@router.patch("/messages/{message_id}")
def edit_message(
    message_id: int, body: str = Body(embed=True), ctx: RequestContext = Depends(authenticated_any)
) -> dict[str, Any]:
    msg = _own_message(ctx, message_id)
    msg.write({"body": body})
    return serialize_messages(ctx, msg)[0]


@router.delete("/messages/{message_id}", status_code=204)
def delete_message(message_id: int, ctx: RequestContext = Depends(authenticated_any)) -> None:
    _own_message(ctx, message_id).unlink()


@router.post("/messages/{message_id}/reactions")
def toggle_reaction(
    message_id: int, data: ReactionIn, ctx: RequestContext = Depends(authenticated_any)
) -> dict[str, Any]:
    msg = ctx.env["chatter.message"].elevated().browse([message_id]).exists()
    if not msg:
        raise MissingError("Message not found")
    check_thread_access(ctx.env, msg.model, msg.res_id, "read")
    if msg.is_internal and not is_internal_user(ctx.env):
        raise MissingError("Message not found")
    reactions = ctx.env["chatter.reaction"].elevated()
    existing = reactions.search(
        [("message_id", "=", message_id), ("user_id", "=", ctx.uid), ("emoji", "=", data.emoji)]
    )
    if existing:
        existing.unlink()
    else:
        reactions.create({"message_id": message_id, "user_id": ctx.uid, "emoji": data.emoji})
    msg.env.cache.invalidate("chatter.message", None, [message_id])
    return serialize_messages(ctx, msg)[0]


# -- followers --------------------------------------------------------------------------------------


@router.get("/{model}/{res_id}/followers")
def list_followers(model: str, res_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    check_thread_access(ctx.env, model, res_id, "read")
    followers = ctx.env["chatter.follower"].elevated().search([("model", "=", model), ("res_id", "=", res_id)])
    items = [
        {"id": f.ids[0], "user_id": f.user_id.ids[0], "name": f.user_id.name, "subtypes": f.subtype_ids.ids}
        for f in followers
    ]
    subtypes = ctx.env["chatter.subtype"].elevated().search([])
    return {
        "items": items,
        "following": any(i["user_id"] == ctx.uid for i in items),
        "subtypes": [
            {"id": s.ids[0], "name": s.name, "internal": s.internal}
            for s in subtypes
            if not s.model_name or s.model_name == model
        ],
    }


@router.post("/{model}/{res_id}/followers")
def follow(
    model: str,
    res_id: int,
    user_ids: list[int] | None = Body(None, embed=True),
    subtype_ids: list[int] | None = Body(None, embed=True),
    ctx: RequestContext = Depends(authenticated),
) -> dict[str, Any]:
    targets = user_ids or [ctx.uid]
    check_thread_access(ctx.env, model, res_id, "write" if any(u != ctx.uid for u in targets) else "read")
    subscribe(ctx.env, model, res_id, targets, subtype_ids)
    return list_followers(model, res_id, ctx)


@router.delete("/{model}/{res_id}/followers")
def unfollow(
    model: str, res_id: int, user_id: int | None = None, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    target = user_id or ctx.uid
    check_thread_access(ctx.env, model, res_id, "write" if target != ctx.uid else "read")
    ctx.env["chatter.follower"].elevated().search(
        [("model", "=", model), ("res_id", "=", res_id), ("user_id", "=", target)]
    ).unlink()
    return list_followers(model, res_id, ctx)


# -- activities -------------------------------------------------------------------------------------


def serialize_activities(activities: Any) -> list[dict[str, Any]]:
    return [
        {
            "id": a.ids[0],
            "model": a.model,
            "res_id": a.res_id,
            "res_name": a.res_name,
            "type": {"id": a.activity_type_id.ids[0], "name": a.activity_type_id.name, "icon": a.activity_type_id.icon},
            "category": a.category,
            "summary": a.summary,
            "note": a.note,
            "date_deadline": a.date_deadline.isoformat(),
            "state": a.state,
            "user": {"id": a.user_id.ids[0], "name": a.user_id.name},
        }
        for a in activities
    ]


@router.get("/{model}/{res_id}/activities")
def list_activities(model: str, res_id: int, ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    check_thread_access(ctx.env, model, res_id, "read")
    acts = (
        ctx.env["chatter.activity"]
        .elevated()
        .search([("model", "=", model), ("res_id", "=", res_id), ("done", "=", False)])
    )
    return serialize_activities(acts)


@router.post("/{model}/{res_id}/activities", status_code=201)
def create_activity(
    model: str, res_id: int, data: ActivityIn, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    check_thread_access(ctx.env, model, res_id, "write")
    act = schedule_activity(
        ctx.env, model, res_id, data.activity_type, data.summary, data.date_deadline, data.user_id, data.note
    )
    return serialize_activities(act.elevated())[0]


def _activity_for_update(ctx: RequestContext, activity_id: int) -> Any:
    act = ctx.env["chatter.activity"].elevated().browse([activity_id]).exists()
    if not act:
        raise MissingError("Activity not found")
    if act.user_id.ids[0] != ctx.uid:
        check_thread_access(ctx.env, act.model, act.res_id, "write")
    else:
        check_thread_access(ctx.env, act.model, act.res_id, "read")
    return act


@router.post("/activities/{activity_id}/done")
def activity_done(
    activity_id: int, feedback: str | None = Body(None, embed=True), ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    act = _activity_for_update(ctx, activity_id)
    act.action_done(feedback)
    return {"ok": True}


@router.post("/activities/{activity_id}/snooze")
def activity_snooze(
    activity_id: int, days: int = Body(1, embed=True), ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    act = _activity_for_update(ctx, activity_id)
    act.action_snooze(days)
    return serialize_activities(act)[0]


@router.delete("/activities/{activity_id}", status_code=204)
def activity_cancel(activity_id: int, ctx: RequestContext = Depends(authenticated)) -> None:
    _activity_for_update(ctx, activity_id).unlink()


@router.get("/activities/mine")
def my_activities(
    state: str | None = Query(None, pattern="^(overdue|today|planned)$"), ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    """The activity dashboard: counts per model and state, plus the list (optionally filtered)."""
    acts = ctx.env["chatter.activity"].search(
        [("user_id", "=", ctx.uid), ("done", "=", False)], order="date_deadline, id"
    )
    summary: dict[str, dict[str, Any]] = {}
    for a in acts:
        entry = summary.setdefault(
            a.model,
            {
                "model": a.model,
                "label": ctx.registry.models[a.model]._description if a.model in ctx.registry.models else a.model,
                "overdue": 0,
                "today": 0,
                "planned": 0,
            },
        )
        entry[a.state] += 1
    listed = acts.filtered(lambda a: a.state == state) if state else acts
    return {"summary": list(summary.values()), "items": serialize_activities(listed[:200])}


# -- mentions ---------------------------------------------------------------------------------------


@router.get("/mentions")
def mention_suggestions(
    q: str = Query("", max_length=64), ctx: RequestContext = Depends(authenticated)
) -> list[dict[str, Any]]:
    domain: list[Any] = [("share", "=", False)]
    if q:
        domain.append({"any": [["name", "ilike", q], ["login", "ilike", q]]})
    users = ctx.env["core.user"].elevated().search(domain, limit=8, order="name")
    return [{"id": u.ids[0], "name": u.name, "login": u.login} for u in users]


# -- inbound email ----------------------------------------------------------------------------------


@router.post("/inbound")
async def inbound_email(request: Request, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    """Raw RFC 822 message from an MTA. Authenticated with ``X-Tessera-Inbound-Token``
    (``TESSERA_MAIL_INBOUND_TOKEN``) or an administrator API key/session."""
    from starlette.concurrency import run_in_threadpool

    from modules.chatter.inbound import process_email

    token = get_settings().mail_inbound_token
    supplied = request.headers.get("x-tessera-inbound-token")
    if not (token and supplied and hmac.compare_digest(token, supplied)):
        if not ctx.authenticated:
            raise AccessDenied("Inbound token or administrator credentials required")
        admin_only(authenticated(ctx))
    raw = await request.body()
    if len(raw) > 25 * 1024 * 1024:
        raise UserError("Message too large")
    return await run_in_threadpool(process_email, ctx.env, raw)
