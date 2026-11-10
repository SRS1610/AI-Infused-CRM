"""Events, attendees and rooms."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from modules.calendar.recurrence import normalize_rule, occurrences
from tessera.exceptions import ConflictError, UserError, ValidationError
from tessera.orm import Model, api, fields

ATTENDEE_STATES = [
    ("needs_action", "Needs action"),
    ("accepted", "Accepted"),
    ("tentative", "Tentative"),
    ("declined", "Declined"),
]
CONFLICT_HORIZON = dt.timedelta(days=366)


def aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


class Room(Model):
    _name = "calendar.room"
    _description = "Meeting room"
    _order = "name"

    name = fields.Char(required=True)
    capacity = fields.Integer(default=4)
    location = fields.Char()
    equipment = fields.Char(help="Screen, video conferencing, whiteboard …")
    company_id = fields.Many2one("core.company", ondelete="cascade")
    active = fields.Boolean(default=True)


class Event(Model):
    _name = "calendar.event"
    _description = "Meeting"
    _order = "start desc, id"
    _inherit = ["chatter.thread"]
    _track_create = False
    _global_search = ["name", "location"]
    _indexes = [("start_stop", ["start", "stop"]), ("room_time", ["room_id", "start"])]

    name = fields.Char("Subject", required=True, tracking=True)
    start = fields.Datetime(required=True, index=True, tracking=True)
    stop = fields.Datetime(required=True, tracking=True)
    allday = fields.Boolean("All day")
    duration = fields.Float("Duration (hours)", compute="_compute_duration", store=True)
    location = fields.Char(tracking=True)
    description = fields.Html()
    user_id = fields.Many2one(
        "core.user",
        "Organizer",
        default=lambda self: self.env.uid if self.env.uid > 1 else None,
        ondelete="set_null",
        index=True,
    )
    attendee_ids = fields.One2many("calendar.attendee", "event_id", "Attendees")
    partner_ids = fields.Many2many(
        "contacts.partner",
        relation="calendar_event__partner_ids",
        column1="event_id",
        column2="partner_id",
        compute="_compute_partner_ids",
        store=True,
        string="Participants",
    )
    privacy = fields.Selection(
        [("public", "Everyone"), ("private", "Participants only")], default="public", required=True
    )
    show_as = fields.Selection([("busy", "Busy"), ("free", "Free")], default="busy", required=True)
    room_id = fields.Many2one("calendar.room", "Room", ondelete="set_null", tracking=True)
    rrule = fields.Char("Recurrence", help="RFC 5545 rule, e.g. FREQ=WEEKLY;BYDAY=MO,WE;COUNT=10")
    exdates = fields.Json(default=list, help="Cancelled occurrences of the series (ISO datetimes)")
    recurrence_id = fields.Many2one(
        "calendar.event", "Series", ondelete="cascade", index=True, help="Set on a modified occurrence"
    )
    recurrence_date = fields.Datetime(help="Original start of the occurrence this record replaces")
    res_model = fields.Char("Linked document model")
    res_id = fields.Integer("Linked document id")
    sync_uid = fields.Char(
        "iCalendar UID", index=True, copy=False, default=lambda self: f"{secrets.token_hex(12)}@tessera"
    )
    company_id = fields.Many2one("core.company", ondelete="cascade")
    active = fields.Boolean(default=True)

    @api.depends("start", "stop")
    def _compute_duration(self) -> None:
        for ev in self:
            ev.duration = round((ev.stop - ev.start).total_seconds() / 3600, 2) if ev.start and ev.stop else 0.0

    @api.depends("attendee_ids.partner_id")
    def _compute_partner_ids(self) -> None:
        for ev in self:
            ev.partner_ids = [a.partner_id.ids[0] for a in ev.attendee_ids if a.partner_id]

    @api.constrains("start", "stop", "rrule")
    def _check_times(self) -> None:
        for ev in self:
            if ev.stop and ev.start and ev.stop < ev.start:
                raise ValidationError("The meeting ends before it starts", field="stop")
            if ev.rrule:
                normalize_rule(ev.rrule)
                if ev.recurrence_id:
                    raise ValidationError("A modified occurrence cannot itself repeat", field="rrule")

    @api.constrains("room_id", "start", "stop", "rrule", "exdates", "active")
    def _check_room(self) -> None:
        for ev in self:
            if ev.room_id and ev.active and ev.show_as == "busy":
                clash = ev._room_conflict()
                if clash is not None:
                    other, when = clash
                    raise ConflictError(
                        f"{ev.room_id.name} is already booked for '{other.name}' on {when:%Y-%m-%d %H:%M} UTC",
                        field="room_id",
                        conflicting_event=other.ids[0],
                    )

    # -- occurrences -------------------------------------------------------------------------------------

    def occurrence_spans(
        self, window_start: dt.datetime, window_end: dt.datetime
    ) -> list[tuple[dt.datetime, dt.datetime]]:
        """(start, stop) of this event's occurrences overlapping the window (series expanded, exceptions removed)."""
        self.ensure_one()
        length = aware(self.stop) - aware(self.start)
        if not self.rrule:
            s, e = aware(self.start), aware(self.stop)
            return [(s, e)] if s < window_end and e > window_start else []
        replaced = [
            aware(r.recurrence_date)
            for r in self.elevated().search([("recurrence_id", "=", self.ids[0])])
            if r.recurrence_date
        ]
        excluded = [dt.datetime.fromisoformat(x) for x in (self.exdates or [])] + replaced
        starts = occurrences(aware(self.start), self.rrule, window_start - length, window_end, excluded)
        return [(s, s + length) for s in starts if s + length > window_start]

    def _room_conflict(self) -> tuple[Model, dt.datetime] | None:
        self.ensure_one()
        first = aware(self.start)
        horizon_end = first + (CONFLICT_HORIZON if self.rrule else (aware(self.stop) - first) + dt.timedelta(seconds=1))
        mine = self.occurrence_spans(first, horizon_end)
        if not mine:
            return None
        lo, hi = min(s for s, _ in mine), max(e for _, e in mine)
        others = (
            self.elevated()
            .with_context(active_test=True)
            .search(
                [
                    ("room_id", "=", self.room_id.ids[0]),
                    ("id", "!=", self.ids[0]),
                    ("show_as", "=", "busy"),
                    {
                        "any": [
                            ["rrule", "!=", None],
                            {"all": [["start", "<", hi.isoformat()], ["stop", ">", lo.isoformat()]]},
                        ]
                    },
                ]
            )
        )
        series_root = self.recurrence_id.ids[0] if self.recurrence_id else None
        for other in others:
            if series_root and other.ids[0] == series_root:
                continue  # an exception never clashes with its own series' original slot
            for s2, e2 in other.occurrence_spans(lo, hi):
                for s1, e1 in mine:
                    if s1 < e2 and s2 < e1:
                        return other, max(s1, s2)
        return None

    # -- attendees ---------------------------------------------------------------------------------------

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("rrule"):
                vals["rrule"] = normalize_rule(vals["rrule"])
            if vals.get("allday") and vals.get("start") and not vals.get("stop"):
                vals["stop"] = vals["start"]
        events = super().create(batch)
        attendees = self.env["calendar.attendee"].elevated("organizer attends")
        for ev in events:  # the organizer attends (accepted)
            if (
                ev.user_id
                and ev.user_id.elevated().partner_id
                and not ev.attendee_ids.filtered(lambda a, e=ev: a.partner_id == e.user_id.elevated().partner_id)
            ):
                attendees.create(
                    {"event_id": ev.ids[0], "partner_id": ev.user_id.elevated().partner_id.ids[0], "state": "accepted"}
                )
        return events

    def write(self, vals):  # type: ignore[no-untyped-def]
        if vals.get("rrule"):
            vals = {**vals, "rrule": normalize_rule(vals["rrule"])}
        return super().write(vals)

    @api.expose
    def action_add_attendees(self, partner_ids: list[int]) -> bool:
        self.ensure_one()
        attendees = self.env["calendar.attendee"].elevated("invite")
        existing = set(self.attendee_ids.mapped("partner_id").ids)
        for pid in partner_ids:
            if pid not in existing:
                attendees.create({"event_id": self.ids[0], "partner_id": pid})
        return True

    @api.expose
    def action_send_invitations(self) -> dict[str, int]:
        """Email every attendee (except the organizer) an invitation with an .ics and RSVP links."""
        from modules.calendar.ics import event_to_ics
        from tessera.config import get_settings

        self.ensure_one()
        base = get_settings().public_url.rstrip("/")
        sent = 0
        ics = event_to_ics(self.elevated(), method="REQUEST")
        organizer = self.user_id.elevated().partner_id if self.user_id else None
        for att in self.elevated().attendee_ids:
            if not att.partner_id.email or (organizer and att.partner_id == organizer):
                continue
            ics_att = (
                self.env["core.attachment"]
                .elevated()
                .create(
                    {
                        "name": "invite.ics",
                        "mimetype": "text/calendar",
                        "raw": ics.encode(),
                        "model": self._name,
                        "res_id": self.ids[0],
                    }
                )
            )
            links = " · ".join(
                f'<a href="{base}/api/v1/calendar/rsvp/{att.access_token}?answer={k}">{label}</a>'
                for k, label in (("accepted", "Yes"), ("tentative", "Maybe"), ("declined", "No"))
            )
            when = f"{self.start:%A %d %B %Y %H:%M} UTC" if not self.allday else f"{self.start:%A %d %B %Y}"
            where = f"<br/>{self.location}" if self.location else ""
            self.env["core.mail_queue"].queue(
                {
                    "email_to": att.partner_id.email,
                    "subject": f"Invitation: {self.name} — {when}",
                    "body_html": f"<p><b>{self.name}</b><br/>{when}{where}</p><p>Will you attend? {links}</p>",
                    "model": self._name,
                    "res_id": self.ids[0],
                    "attachment_ids": [("set", ics_att.ids)],
                }
            )
            sent += 1
        return {"sent": sent}

    @api.expose
    def action_cancel_occurrence(self, occurrence_start: str) -> bool:
        """Remove one occurrence from a series (adds an EXDATE)."""
        self.ensure_one()
        if not self.rrule:
            raise UserError("This meeting does not repeat")
        when = aware(dt.datetime.fromisoformat(occurrence_start)).isoformat()
        self.write({"exdates": sorted({*(self.exdates or []), when})})
        return True

    @api.expose
    def action_edit_occurrence(self, occurrence_start: str, values: dict[str, Any]) -> dict[str, int]:
        """Detach one occurrence as its own event (the series keeps the others)."""
        self.ensure_one()
        if not self.rrule:
            raise UserError("This meeting does not repeat")
        original = aware(dt.datetime.fromisoformat(occurrence_start))
        length = aware(self.stop) - aware(self.start)
        vals = {
            "name": self.name,
            "start": original,
            "stop": original + length,
            "location": self.location,
            "room_id": self.room_id.ids[0] if self.room_id else None,
            "user_id": self.user_id.ids[0] if self.user_id else None,
            "privacy": self.privacy,
            "recurrence_id": self.ids[0],
            "recurrence_date": original,
            "sync_uid": self.sync_uid,
        }
        vals.update(
            {k: v for k, v in values.items() if k in ("name", "start", "stop", "location", "room_id", "description")}
        )
        exception = self.create(vals)
        for att in self.attendee_ids:
            if not exception.attendee_ids.filtered(lambda a, p=att.partner_id: a.partner_id == p):
                self.env["calendar.attendee"].elevated().create(
                    {"event_id": exception.ids[0], "partner_id": att.partner_id.ids[0], "state": att.state}
                )
        return {"event_id": exception.ids[0]}


class Attendee(Model):
    _name = "calendar.attendee"
    _description = "Attendee"
    _rec_name = "partner_id"
    _sql_constraints = [("event_partner_uniq", "UNIQUE (event_id, partner_id)", "Already invited")]

    event_id = fields.Many2one("calendar.event", required=True, ondelete="cascade", index=True)
    partner_id = fields.Many2one("contacts.partner", required=True, ondelete="cascade", index=True)
    state = fields.Selection(ATTENDEE_STATES, default="needs_action", required=True)
    access_token = fields.Char(
        copy=False, index=True, default=lambda self: secrets.token_urlsafe(18), groups="core.group_system"
    )
    responded_at = fields.Datetime(readonly=True)


def expand(
    env: Any, window_start: dt.datetime, window_end: dt.datetime, domain: list[Any] | None = None
) -> list[dict[str, Any]]:
    """Every occurrence visible to the caller in the window, series expanded (calendar views and free/busy)."""
    events = env["calendar.event"].search(
        [
            *(domain or []),
            {
                "any": [
                    ["rrule", "!=", None],
                    {"all": [["start", "<", window_end.isoformat()], ["stop", ">", window_start.isoformat()]]},
                ]
            },
        ]
    )
    out = []
    for ev in events:
        for s, e in ev.occurrence_spans(window_start, window_end):
            out.append(
                {
                    "id": ev.ids[0],
                    "name": ev.name,
                    "start": s.isoformat(),
                    "stop": e.isoformat(),
                    "allday": ev.allday,
                    "recurring": bool(ev.rrule),
                    "room": ev.room_id.name if ev.room_id else None,
                    "location": ev.location,
                    "show_as": ev.show_as,
                    "organizer_id": ev.user_id.ids[0] if ev.user_id else None,
                }
            )
    return sorted(out, key=lambda o: o["start"])
