"""Scheduling links (public booking pages) and external calendar sync accounts."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from tessera.exceptions import ValidationError
from tessera.orm import Model, api, fields
from tessera.security import crypto

DEFAULT_AVAILABILITY = [{"weekday": d, "start": "09:00", "end": "17:00"} for d in range(5)]


class BookingLink(Model):
    _name = "calendar.booking_link"
    _description = "Scheduling link"
    _order = "name"

    name = fields.Char(required=True, help="What people book, e.g. '30 min intro call'")
    user_id = fields.Many2one("core.user", "Host", required=True, default=lambda self: self.env.uid, ondelete="cascade")
    slug = fields.Char(required=True, index=True, copy=False, default=lambda self: secrets.token_urlsafe(9))
    duration_minutes = fields.Integer(default=30, required=True)
    buffer_minutes = fields.Integer(default=0, help="Free time kept around existing meetings")
    days_ahead = fields.Integer(default=30, help="How far ahead people can book")
    min_notice_hours = fields.Integer(default=4)
    availability = fields.Json(
        default=lambda self: list(DEFAULT_AVAILABILITY),
        help='[{"weekday": 0, "start": "09:00", "end": "17:00"}] (0 = Monday)',
    )
    timezone = fields.Char(default="UTC")
    location = fields.Char()
    room_id = fields.Many2one("calendar.room", ondelete="set_null")
    active = fields.Boolean(default=True)
    _sql_constraints = [("slug_uniq", "UNIQUE (slug)", "Scheduling link addresses are unique")]

    @api.constrains("availability", "duration_minutes", "timezone")
    def _check(self) -> None:
        for link in self:
            if (link.duration_minutes or 0) < 5:
                raise ValidationError("Meetings last at least 5 minutes", field="duration_minutes")
            try:
                ZoneInfo(link.timezone or "UTC")
            except Exception as exc:
                raise ValidationError(f"Unknown time zone '{link.timezone}'", field="timezone") from exc
            for window in link.availability or []:
                try:
                    if not 0 <= int(window["weekday"]) <= 6:
                        raise ValueError
                    s, e = dt.time.fromisoformat(window["start"]), dt.time.fromisoformat(window["end"])
                    if e <= s:
                        raise ValueError
                except (KeyError, ValueError, TypeError) as exc:
                    raise ValidationError(f"Invalid availability window {window}", field="availability") from exc

    def free_slots(self, start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
        """Slot starts in [start, end) inside the availability windows that don't clash with the host's busy
        time (their meetings plus buffer) and respect the notice period and booking horizon."""
        from modules.calendar.models.event import expand

        self.ensure_one()
        tz = ZoneInfo(self.timezone or "UTC")
        now = dt.datetime.now(dt.UTC)
        start = max(start, now + dt.timedelta(hours=self.min_notice_hours or 0))
        end = min(end, now + dt.timedelta(days=self.days_ahead or 30))
        if end <= start:
            return []
        host = self.user_id.elevated()
        busy_events = expand(
            self.env.elevated(),
            start - dt.timedelta(days=1),
            end + dt.timedelta(days=1),
            [
                ("show_as", "=", "busy"),
                {
                    "any": [
                        ["user_id", "=", host.ids[0]],
                        ["partner_ids", "=", host.partner_id.ids[0] if host.partner_id else 0],
                    ]
                },
            ],
        )
        pad = dt.timedelta(minutes=self.buffer_minutes or 0)
        busy = [
            (dt.datetime.fromisoformat(e["start"]) - pad, dt.datetime.fromisoformat(e["stop"]) + pad)
            for e in busy_events
        ]
        length = dt.timedelta(minutes=self.duration_minutes)
        slots: list[dt.datetime] = []
        day = start.astimezone(tz).date()
        while dt.datetime.combine(day, dt.time(0), tz) < end:
            for window in self.availability or []:
                if int(window["weekday"]) != day.weekday():
                    continue
                cursor = dt.datetime.combine(day, dt.time.fromisoformat(window["start"]), tz)
                stop = dt.datetime.combine(day, dt.time.fromisoformat(window["end"]), tz)
                while cursor + length <= stop:
                    s_utc = cursor.astimezone(dt.UTC)
                    e_utc = s_utc + length
                    if (
                        s_utc >= start
                        and e_utc <= end + length
                        and not any(s_utc < b_end and b_start < e_utc for b_start, b_end in busy)
                    ):
                        slots.append(s_utc)
                    cursor += length
            day += dt.timedelta(days=1)
        return sorted(slots)


class SyncAccount(Model):
    _name = "calendar.sync_account"
    _description = "External calendar"
    _order = "user_id, provider"

    user_id = fields.Many2one(
        "core.user", required=True, default=lambda self: self.env.uid, ondelete="cascade", index=True
    )
    provider = fields.Selection(
        [("caldav", "CalDAV"), ("google", "Google Calendar"), ("microsoft", "Microsoft 365")], required=True
    )
    name = fields.Char(required=True, default="My calendar")
    url = fields.Char(help="CalDAV collection URL")
    username = fields.Char()
    secret = fields.Char(
        help="Password (CalDAV) or OAuth refresh/access token; stored encrypted", groups="core.group_system"
    )
    calendar_id = fields.Char(help="Remote calendar id (Google, Microsoft); default 'primary'")
    sync_token = fields.Char(readonly=True, copy=False)
    last_sync = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    state = fields.Selection(
        [("active", "Active"), ("error", "Error"), ("paused", "Paused")], default="active", required=True
    )
    link_ids = fields.One2many("calendar.sync_link", "account_id", "Synchronised events")

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("secret") and not crypto.is_encrypted(vals["secret"]):
                vals["secret"] = crypto.encrypt(vals["secret"])
        return super().create(batch)

    def write(self, vals):  # type: ignore[no-untyped-def]
        vals = dict(vals)
        if vals.get("secret") and not crypto.is_encrypted(vals["secret"]):
            vals["secret"] = crypto.encrypt(vals["secret"])
        return super().write(vals)

    @api.expose
    def action_sync_now(self) -> dict[str, Any]:
        from modules.calendar.sync.engine import sync_account

        self.ensure_one()
        return sync_account(self.elevated())


class SyncLink(Model):
    _name = "calendar.sync_link"
    _description = "Event ↔ remote event"
    _rec_name = "remote_uid"
    _studio = False
    _sql_constraints = [("account_uid_uniq", "UNIQUE (account_id, remote_uid)", "One link per remote event")]

    account_id = fields.Many2one("calendar.sync_account", required=True, ondelete="cascade", index=True)
    event_id = fields.Many2one("calendar.event", ondelete="set_null", index=True)
    remote_uid = fields.Char(required=True)
    etag = fields.Char()
    synced_write_date = fields.Datetime(help="Local write_date when last pushed or pulled")
    content_hash = fields.Char(help="Hash of the synchronised fields as last pushed or pulled")
