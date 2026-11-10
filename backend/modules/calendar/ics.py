"""iCalendar (RFC 5545) conversion for invitations and CalDAV."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from icalendar import Calendar, vRecur
from icalendar import Event as VEvent

if TYPE_CHECKING:
    from tessera.orm.model import Model


@dataclass
class RemoteEvent:
    """Provider-neutral event exchanged with sync adapters."""

    uid: str
    summary: str
    start: dt.datetime
    end: dt.datetime
    allday: bool = False
    location: str | None = None
    description: str | None = None
    rrule: str | None = None
    exdates: list[dt.datetime] = field(default_factory=list)
    attendees: list[str] = field(default_factory=list)
    etag: str | None = None
    href: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def content_hash(event: RemoteEvent) -> str:
    """Fingerprint of what we synchronise (title, times, place, notes, recurrence)."""
    import hashlib

    parts = [
        event.summary or "",
        event.start.astimezone(dt.UTC).isoformat(),
        event.end.astimezone(dt.UTC).isoformat(),
        str(event.allday),
        event.location or "",
        event.description or "",
        (event.rrule or "").upper(),
        ",".join(sorted(e.astimezone(dt.UTC).isoformat() for e in event.exdates)),
    ]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _utc(value: Any) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    return dt.datetime.combine(value, dt.time(0), dt.UTC)


def from_event(event: Model) -> RemoteEvent:
    return RemoteEvent(
        uid=event.sync_uid,
        summary=event.name,
        start=_utc(event.start),
        end=_utc(event.stop),
        allday=bool(event.allday),
        location=event.location or None,
        description=event.description or None,
        rrule=event.rrule or None,
        exdates=[_utc(dt.datetime.fromisoformat(x)) for x in (event.exdates or [])],
        attendees=[a.partner_id.email for a in event.attendee_ids if a.partner_id.email],
    )


def to_ics(remote: RemoteEvent, method: str | None = None) -> str:
    cal = Calendar()
    cal.add("prodid", "-//Tessera//Calendar//EN")
    cal.add("version", "2.0")
    if method:
        cal.add("method", method)
    ev = VEvent()
    ev.add("uid", remote.uid)
    ev.add("summary", remote.summary)
    ev.add("dtstamp", dt.datetime.now(dt.UTC))
    if remote.allday:
        ev.add("dtstart", remote.start.date())
        ev.add("dtend", max(remote.end.date(), remote.start.date() + dt.timedelta(days=1)))
    else:
        ev.add("dtstart", remote.start)
        ev.add("dtend", remote.end)
    if remote.location:
        ev.add("location", remote.location)
    if remote.description:
        ev.add("description", remote.description)
    if remote.rrule:
        ev.add("rrule", vRecur.from_ical(remote.rrule))
    for ex in remote.exdates:
        ev.add("exdate", ex)
    for email in remote.attendees:
        ev.add("attendee", f"mailto:{email}")
    cal.add_component(ev)
    return cal.to_ical().decode()


def event_to_ics(event: Model, method: str | None = None) -> str:
    return to_ics(from_event(event), method)


def from_ics(text: str | bytes, etag: str | None = None, href: str | None = None) -> list[RemoteEvent]:
    """Masters and overridden occurrences (RECURRENCE-ID kept in ``extra``)."""
    cal = Calendar.from_ical(text)
    out = []
    for comp in cal.walk("VEVENT"):
        start = comp.decoded("dtstart")
        end = comp.decoded("dtend") if comp.get("dtend") else start
        allday = not isinstance(start, dt.datetime)
        rrule = comp.get("rrule")
        exdates: list[dt.datetime] = []
        for ex in (
            comp.get("exdate", [])
            if isinstance(comp.get("exdate"), list)
            else ([comp.get("exdate")] if comp.get("exdate") else [])
        ):
            exdates.extend(_utc(d.dt) for d in ex.dts)
        extra: dict[str, Any] = {}
        if comp.get("recurrence-id"):
            extra["recurrence_id"] = _utc(comp.decoded("recurrence-id"))
        out.append(
            RemoteEvent(
                uid=str(comp.get("uid")),
                summary=str(comp.get("summary", "")),
                start=_utc(start),
                end=_utc(end),
                allday=allday,
                location=str(comp.get("location")) if comp.get("location") else None,
                description=str(comp.get("description")) if comp.get("description") else None,
                rrule=rrule.to_ical().decode() if rrule else None,
                exdates=exdates,
                attendees=[
                    str(a).removeprefix("mailto:").removeprefix("MAILTO:")
                    for a in (
                        comp.get("attendee")
                        if isinstance(comp.get("attendee"), list)
                        else ([comp.get("attendee")] if comp.get("attendee") else [])
                    )
                ],
                etag=etag,
                href=href,
                extra=extra,
            )
        )
    return out
