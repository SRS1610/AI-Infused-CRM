"""Calendar API: expanded occurrences, RSVP (in-app and by email link), public booking pages."""

from __future__ import annotations

import datetime as dt
import hmac
import html
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from modules.calendar.models.event import expand
from tessera.api.deps import RequestContext, authenticated, request_context
from tessera.api.middleware import limiter
from tessera.api.registry import register_router
from tessera.exceptions import MissingError, UserError
from tessera.i18n import _
from tessera.runtime import current_remote_ip

router = register_router(APIRouter(prefix="/calendar", tags=["calendar"]))
ANSWERS = {"accepted", "tentative", "declined"}


def _dt(value: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UserError(_("Use ISO 8601 date-times")) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


@router.get("/events")
def events(
    start: str,
    end: str,
    user_id: int | None = None,
    room_id: int | None = None,
    ctx: RequestContext = Depends(authenticated),
) -> list[dict[str, Any]]:
    """Occurrences in [start, end) visible to the caller, recurring series expanded."""
    s, e = _dt(start), _dt(end)
    if e <= s or e - s > dt.timedelta(days=370):
        raise UserError(_("Ask for at most one year"))
    domain: list[Any] = []
    if user_id:
        partner = ctx.env["core.user"].elevated().browse([user_id]).partner_id
        domain.append({"any": [["user_id", "=", user_id], ["partner_ids", "=", partner.ids[0] if partner else 0]]})
    if room_id:
        domain.append(("room_id", "=", room_id))
    return expand(ctx.env, s, e, domain)


@router.post("/events/{event_id}/rsvp")
def rsvp(
    event_id: int, answer: str = Body(..., embed=True), ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    if answer not in ANSWERS:
        raise UserError(_("Answer accepted, tentative or declined"))
    partner = ctx.env.user.elevated().partner_id
    att = (
        ctx.env["calendar.attendee"]
        .elevated()
        .search([("event_id", "=", event_id), ("partner_id", "=", partner.ids[0] if partner else 0)], limit=1)
    )
    if not att:
        raise MissingError(_("You are not invited to this meeting"))
    att.write({"state": answer, "responded_at": ctx.env.now()})
    return {"state": answer}


@router.get("/rsvp/{token}", response_class=HTMLResponse)
def rsvp_link(token: str, answer: str = Query(...), ctx: RequestContext = Depends(request_context)) -> HTMLResponse:
    """The Yes / Maybe / No links in invitation emails (no login)."""
    if not limiter.allow(f"rsvp:{current_remote_ip() or '?'}", 30):
        raise UserError(_("Too many attempts"))
    if answer not in ANSWERS:
        raise UserError(_("Unknown answer"))
    attendees = ctx.env["calendar.attendee"].elevated("rsvp link")
    att = attendees.search([("access_token", "=", token)], limit=1)
    if not att or not hmac.compare_digest(att.access_token or "", token):
        raise MissingError(_("This invitation link is not valid"))
    att.write({"state": answer, "responded_at": ctx.env.now()})
    label = {"accepted": "You're attending", "tentative": "You might attend", "declined": "You declined"}[answer]
    event = att.event_id
    title = html.escape(event.name)
    body = (
        f"<!doctype html><meta charset=utf-8><title>{title}</title>"
        "<main style='font-family:system-ui;margin:3rem auto;max-width:30rem'>"
        f"<h1>{label}</h1><p>{title} — {event.start:%A %d %B %Y %H:%M} UTC</p></main>"
    )
    return HTMLResponse(body)


def _link(ctx: RequestContext, slug: str) -> Any:
    link = (
        ctx.env["calendar.booking_link"]
        .elevated("booking page")
        .search([("slug", "=", slug), ("active", "=", True)], limit=1)
    )
    if not link:
        raise MissingError(_("This booking page does not exist"))
    return link


@router.get("/book/{slug}")
def booking_page(slug: str, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    link = _link(ctx, slug)
    return {
        "name": link.name,
        "host": link.user_id.name,
        "duration_minutes": link.duration_minutes,
        "timezone": link.timezone,
        "location": link.location,
    }


@router.get("/book/{slug}/slots")
def booking_slots(slug: str, start: str, end: str, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    link = _link(ctx, slug)
    s, e = _dt(start), _dt(end)
    if e - s > dt.timedelta(days=31):
        raise UserError(_("Ask for at most 31 days"))
    return {"slots": [slot.isoformat() for slot in link.free_slots(s, e)]}


class BookingIn(BaseModel):
    start: str
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(min_length=3, max_length=200)
    note: str | None = Field(None, max_length=2000)


@router.post("/book/{slug}", status_code=201)
def book(slug: str, data: BookingIn, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    if not limiter.allow(f"booking:{current_remote_ip() or '?'}", 10):
        raise UserError(_("Too many bookings from this address; try again later"))
    link = _link(ctx, slug)
    start = _dt(data.start)
    length = dt.timedelta(minutes=link.duration_minutes)
    if start not in link.free_slots(start - dt.timedelta(minutes=1), start + length):
        raise UserError(_("That time is no longer available"))
    env = ctx.env.elevated("public booking")
    from modules.contacts.models.partner import normalize_email

    email = normalize_email(data.email)
    partners = env["contacts.partner"]
    guest = partners.search([("email_normalized", "=", email)], limit=1) or partners.create(
        {"name": data.name, "email": email}
    )
    event = env["calendar.event"].create(
        {
            "name": f"{link.name} with {data.name}",
            "start": start,
            "stop": start + length,
            "user_id": link.user_id.ids[0],
            "location": link.location,
            "room_id": link.room_id.ids[0] if link.room_id else None,
            "description": f"<p>{html.escape(data.note or '')}</p>" if data.note else None,
        }
    )
    env["calendar.attendee"].create({"event_id": event.ids[0], "partner_id": guest.ids[0], "state": "accepted"})
    event.action_send_invitations()
    return {"event_id": event.ids[0], "start": start.isoformat()}


# -- AI: suggested times and meeting briefs (Phase 1.6) --------------------------------------------


class SuggestIn(BaseModel):
    event_id: int | None = None
    partner_ids: list[int] = Field(default_factory=list, max_length=30)
    user_ids: list[int] = Field(default_factory=list, max_length=30)
    duration_minutes: int | None = None
    start: str | None = None
    end: str | None = None
    tz: str | None = None
    day_start: str | None = None
    day_end: str | None = None
    weekdays: list[int] | None = None
    buffer_minutes: int | None = None
    step_minutes: int | None = None
    min_notice_minutes: int | None = None
    prefer: str | None = None
    room_id: int | None = None
    avoid_lunch: bool = False
    respect_attendee_hours: bool = True
    limit: int = Field(5, ge=1, le=20)


@router.post("/ai/suggest-times")
def ai_suggest_times(data: SuggestIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Ranked free slots for the meeting's attendees (or ``partner_ids``) and the caller. Deterministic;
    nothing is booked — moving or creating the meeting stays the user's own action."""
    from modules.calendar.ai_features import SUGGEST, check_feature, suggest_for

    check_feature(ctx.env, SUGGEST)
    record = None
    if data.event_id:
        record = ctx.env["calendar.event"].browse([data.event_id])
        if not record.exists():
            raise MissingError(_("No such meeting"))
    inputs = data.model_dump(exclude_none=True, exclude={"event_id"})
    return suggest_for(ctx.env, inputs, record)


@router.get("/events/{event_id}/brief")
def ai_latest_brief(event_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """The caller's own latest brief of this meeting (prepared before the meeting or on demand)."""
    from modules.calendar.ai_features import BRIEF, check_feature, latest_brief

    check_feature(ctx.env, BRIEF)
    event = ctx.env["calendar.event"].browse([event_id])
    if not event.exists():
        raise MissingError(_("No such meeting"))
    return {"brief": latest_brief(ctx.env, event)}
