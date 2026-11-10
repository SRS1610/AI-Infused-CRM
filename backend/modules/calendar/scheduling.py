"""Free/busy and slot search shared by scheduling links and suggested meeting times.

``busy_intervals`` reads every attendee's busy time (their own meetings and those they attend and
did not decline, recurring series expanded, all-day events covering whole days) with an elevated
environment, exactly like a scheduling link reads its host's calendar: only *when* someone is busy
is used, never what the meeting is. ``slot_starts`` walks availability windows in a time zone.

``suggest_times`` (feature ``calendar.suggest_times``) is a deterministic search: every slot it
returns is free for every attendee and meets the constraints; the ranking prefers earlier slots,
the requested part of the day, everyone's working hours and some breathing room. Booking stays a
user action: the caller only receives slots.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from tessera.exceptions import UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

Span = tuple[dt.datetime, dt.datetime]
WORK_START, WORK_END = dt.time(9), dt.time(17)
MAX_WINDOW = dt.timedelta(days=31)


def aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception as exc:
        raise UserError(_("Unknown time zone '{name}'", name=name)) from exc


def event_spans(ev: Model, start: dt.datetime, end: dt.datetime) -> list[Span]:
    """Occurrences of one event overlapping [start, end); all-day events block their whole days (UTC)."""
    if ev.allday:
        first = aware(dt.datetime.combine(aware(ev.start).date(), dt.time(), dt.UTC))
        last = aware(dt.datetime.combine(aware(ev.stop or ev.start).date(), dt.time(), dt.UTC)) + dt.timedelta(days=1)
        if not ev.rrule:
            return [(first, last)] if first < end and last > start else []
        length = last - first
        return [(s, s + length) for s, _e in ev.occurrence_spans(start - length, end) if s < end and s + length > start]
    return ev.occurrence_spans(start, end)


def busy_intervals(
    env: Environment,
    partner_ids: list[int],
    start: dt.datetime,
    end: dt.datetime,
    *,
    room_id: int | None = None,
    exclude_event_id: int | None = None,
) -> dict[str, list[Span]]:
    """``{"partner:<id>": [...], "room:<id>": [...]}`` busy spans in [start, end)."""
    sys_env = env.elevated("free/busy")
    out: dict[str, list[Span]] = {}
    users = sys_env["core.user"].with_context(active_test=False).search([("partner_id", "in", partner_ids)])
    user_of = {u.partner_id.ids[0]: u.ids[0] for u in users if u.partner_id}
    window = [
        ("show_as", "=", "busy"),
        {
            "any": [
                ["rrule", "!=", None],
                {"all": [["start", "<", end.isoformat()], ["stop", ">=", (start - dt.timedelta(days=1)).isoformat()]]},
            ]
        },
    ]
    for pid in partner_ids:
        who: list[Any] = [["partner_ids", "=", pid]]
        if pid in user_of:
            who.append(["user_id", "=", user_of[pid]])
        events = sys_env["calendar.event"].search([*window, {"any": who}])
        declined = set(
            sys_env["calendar.attendee"]
            .search([("partner_id", "=", pid), ("state", "=", "declined"), ("event_id", "in", events.ids)])
            .mapped("event_id")
            .ids
        )
        spans: list[Span] = []
        for ev in events:
            if ev.ids[0] == exclude_event_id or (ev.ids[0] in declined and ev.user_id.ids[:1] != [user_of.get(pid)]):
                continue
            spans += event_spans(ev, start, end)
        out[f"partner:{pid}"] = sorted(spans)
    if room_id:
        events = sys_env["calendar.event"].search([*window, ("room_id", "=", room_id)])
        out[f"room:{room_id}"] = sorted(
            s for ev in events if ev.ids[0] != exclude_event_id for s in event_spans(ev, start, end)
        )
    return out


def slot_starts(
    tz: ZoneInfo,
    start: dt.datetime,
    end: dt.datetime,
    windows: list[dict[str, Any]],
    length: dt.timedelta,
    step: dt.timedelta,
) -> list[dt.datetime]:
    """UTC starts of slots of ``length`` every ``step`` inside the weekly ``windows`` (``{"weekday",
    "start", "end"}`` in ``tz``) that begin at or after ``start`` and end by ``end``."""
    out: list[dt.datetime] = []
    day = start.astimezone(tz).date()
    while dt.datetime.combine(day, dt.time(0), tz) < end:
        for window in windows:
            if int(window["weekday"]) != day.weekday():
                continue
            cursor = dt.datetime.combine(day, dt.time.fromisoformat(window["start"]), tz)
            stop = dt.datetime.combine(day, dt.time.fromisoformat(window["end"]), tz)
            while cursor + length <= stop:
                s_utc = cursor.astimezone(dt.UTC)
                if s_utc >= start and s_utc + length <= end:
                    out.append(s_utc)
                cursor += step
        day += dt.timedelta(days=1)
    return sorted(set(out))


def overlaps(s: dt.datetime, e: dt.datetime, spans: list[Span], pad: dt.timedelta = dt.timedelta(0)) -> bool:
    return any(s - pad < b_end and b_start < e + pad for b_start, b_end in spans)


# -- suggested meeting times ---------------------------------------------------------------------


@dataclass
class Constraints:
    duration_minutes: int = 30
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    tz: str = "UTC"
    day_start: str = "09:00"
    day_end: str = "17:00"
    weekdays: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])
    buffer_minutes: int = 0
    step_minutes: int = 30
    min_notice_minutes: int = 0
    prefer: str | None = None  # "morning" | "afternoon"
    room_id: int | None = None
    avoid_lunch: bool = False
    respect_attendee_hours: bool = True
    exclude_event_id: int | None = None

    @classmethod
    def parse(cls, env: Environment, data: dict[str, Any]) -> Constraints:
        def when(key: str) -> dt.datetime | None:
            value = data.get(key)
            if not value:
                return None
            if isinstance(value, dt.datetime):
                return aware(value)
            try:
                return aware(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")))
            except ValueError as exc:
                raise UserError(_("'{key}' must be an ISO 8601 date-time", key=key)) from exc

        tz = str(data.get("tz") or env.user.elevated().tz or "UTC")
        zone(tz)
        c = cls(
            duration_minutes=int(data.get("duration_minutes") or 30),
            start=when("start"),
            end=when("end"),
            tz=tz,
            day_start=str(data.get("day_start") or "09:00"),
            day_end=str(data.get("day_end") or "17:00"),
            weekdays=[int(d) for d in (data.get("weekdays") or [0, 1, 2, 3, 4])],
            buffer_minutes=int(data.get("buffer_minutes") or 0),
            step_minutes=int(data.get("step_minutes") or 30),
            min_notice_minutes=int(data.get("min_notice_minutes") or 0),
            prefer=data.get("prefer") or None,
            room_id=int(data["room_id"]) if data.get("room_id") else None,
            avoid_lunch=bool(data.get("avoid_lunch")),
            respect_attendee_hours=data.get("respect_attendee_hours", True) is not False,
            exclude_event_id=int(data["exclude_event_id"]) if data.get("exclude_event_id") else None,
        )
        if not 5 <= c.duration_minutes <= 8 * 60:
            raise UserError(_("A meeting lasts between 5 minutes and 8 hours"))
        if c.step_minutes not in (5, 10, 15, 20, 30, 60):
            raise UserError(_("Slots start every 5, 10, 15, 20, 30 or 60 minutes"))
        if c.prefer not in (None, "morning", "afternoon"):
            raise UserError(_("Prefer 'morning' or 'afternoon'"))
        try:
            if dt.time.fromisoformat(c.day_end) <= dt.time.fromisoformat(c.day_start):
                raise UserError(_("The working day ends before it starts"))
        except ValueError as exc:
            raise UserError(_("Use HH:MM for the working day")) from exc
        return c


def attendee_zones(env: Environment, partner_ids: list[int]) -> dict[int, ZoneInfo]:
    """Known time zones of attendees: their user's (internal people) or the contact's own."""
    sys_env = env.elevated("attendee time zones")
    out: dict[int, ZoneInfo] = {}
    users = sys_env["core.user"].search([("partner_id", "in", partner_ids), ("share", "=", False)])
    for u in users:
        if u.tz:
            out[u.partner_id.ids[0]] = zone(u.tz)
    for p in sys_env["contacts.partner"].browse(partner_ids):
        if p.ids[0] not in out and p.tz:
            out[p.ids[0]] = zone(p.tz)
    return out


def in_working_hours(s: dt.datetime, e: dt.datetime, tz: ZoneInfo) -> bool:
    ls, le = s.astimezone(tz), e.astimezone(tz)
    return ls.date() == le.date() and ls.weekday() < 5 and ls.time() >= WORK_START and le.time() <= WORK_END


def suggest_times(env: Environment, partner_ids: list[int], c: Constraints, *, limit: int = 5) -> dict[str, Any]:
    """Ranked free slots for everyone in ``partner_ids`` (contacts ``env``'s user can read)."""
    partners = env["contacts.partner"].browse(list(dict.fromkeys(partner_ids)))
    partners.check_access("read")  # the user cannot probe the calendars of people they cannot see
    tz = zone(c.tz)
    now = env.now()
    start = aware(c.start) if c.start else now
    end = aware(c.end) if c.end else start + dt.timedelta(days=7)
    if end <= start or end - start > MAX_WINDOW:
        raise UserError(_("Search at most 31 days ahead"))
    start = max(start, now + dt.timedelta(minutes=c.min_notice_minutes))
    length = dt.timedelta(minutes=c.duration_minutes)
    pad = dt.timedelta(minutes=c.buffer_minutes)
    windows = [{"weekday": d, "start": c.day_start, "end": c.day_end} for d in c.weekdays]
    busy = busy_intervals(
        env,
        partners.ids,
        start - dt.timedelta(days=1),
        end + dt.timedelta(days=1),
        room_id=c.room_id,
        exclude_event_id=c.exclude_event_id,
    )
    zones = attendee_zones(env, partners.ids) if c.respect_attendee_hours else {}
    candidates: list[dict[str, Any]] = []
    for s in slot_starts(tz, start, end, windows, length, dt.timedelta(minutes=c.step_minutes)):
        e = s + length
        if any(overlaps(s, e, spans, pad) for spans in busy.values()):
            continue
        local = s.astimezone(tz)
        if c.avoid_lunch:
            lunch_s = dt.datetime.combine(local.date(), dt.time(12), tz)
            if overlaps(s, e, [(lunch_s, lunch_s + dt.timedelta(hours=1))]):
                continue
        if any(not in_working_hours(s, e, z) for z in zones.values()):
            continue
        candidates.append(_rank(s, e, local, start, c, busy, zones, len(partners)))
    candidates.sort(key=lambda x: (-x["score"], x["start"]))
    return {
        "slots": candidates[:limit],
        "searched": {"start": start.isoformat(), "end": end.isoformat(), "tz": c.tz, "attendees": partners.ids},
        "found": len(candidates),
    }


def _rank(
    s: dt.datetime,
    e: dt.datetime,
    local: dt.datetime,
    start: dt.datetime,
    c: Constraints,
    busy: dict[str, list[Span]],
    zones: dict[int, ZoneInfo],
    attendees: int,
) -> dict[str, Any]:
    reasons = [f"All {attendees} attendees are free" if attendees > 1 else "You are free"]
    score = 10.0 - (s - start).total_seconds() / 86400  # sooner is better, a day costs a point
    if (c.prefer == "morning" and local.hour < 12) or (c.prefer == "afternoon" and local.hour >= 12):
        score += 3
        reasons.append(_("In the {period}, as asked", period=c.prefer))
    if zones:
        reasons.append(_("Inside everyone's working hours") if len(zones) > 1 else _("Inside working hours"))
    breathing = dt.timedelta(minutes=15)
    if not any(overlaps(s, e, spans, breathing) for spans in busy.values()):
        score += 0.5
        reasons.append(_("Not back-to-back with another meeting"))
    if c.room_id:
        reasons.append(_("The room is free"))
    return {
        "start": s.isoformat(),
        "stop": e.isoformat(),
        "local": local.strftime("%a %d %b %H:%M"),
        "score": round(score, 3),
        "reasons": reasons,
    }
