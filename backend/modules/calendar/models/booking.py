"""Scheduling links (public booking pages) and external calendar sync accounts."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any
from zoneinfo import ZoneInfo

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields
from tessera.security import crypto

SECRET_FIELDS = ("secret", "refresh_token", "access_token")
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
                raise ValidationError(_("Meetings last at least 5 minutes"), field="duration_minutes")
            try:
                ZoneInfo(link.timezone or "UTC")
            except Exception as exc:
                raise ValidationError(
                    _("Unknown time zone '{timezone}'", timezone=link.timezone), field="timezone"
                ) from exc
            for window in link.availability or []:
                try:
                    if not 0 <= int(window["weekday"]) <= 6:
                        raise ValueError
                    s, e = dt.time.fromisoformat(window["start"]), dt.time.fromisoformat(window["end"])
                    if e <= s:
                        raise ValueError
                except (KeyError, ValueError, TypeError) as exc:
                    raise ValidationError(
                        _("Invalid availability window {window}", window=window), field="availability"
                    ) from exc

    def free_slots(self, start: dt.datetime, end: dt.datetime) -> list[dt.datetime]:
        """Slot starts in [start, end) inside the availability windows that don't clash with the host's busy
        time (their meetings plus buffer) and respect the notice period and booking horizon."""
        from modules.calendar.scheduling import busy_intervals, overlaps, slot_starts

        self.ensure_one()
        tz = ZoneInfo(self.timezone or "UTC")
        now = dt.datetime.now(dt.UTC)
        start = max(start, now + dt.timedelta(hours=self.min_notice_hours or 0))
        end = min(end, now + dt.timedelta(days=self.days_ahead or 30))
        if end <= start:
            return []
        host = self.user_id.elevated()
        partner_id = host.partner_id.ids[0] if host.partner_id else None
        busy = busy_intervals(
            self.env, [partner_id] if partner_id else [], start - dt.timedelta(days=1), end + dt.timedelta(days=1)
        ).get(f"partner:{partner_id}", [])
        pad = dt.timedelta(minutes=self.buffer_minutes or 0)
        length = dt.timedelta(minutes=self.duration_minutes)
        # a slot may start up to the end of the asked range (it can finish after it)
        starts = slot_starts(tz, start, end + length, list(self.availability or []), length, length)
        return [s for s in starts if s < end + length and not overlaps(s, s + length, busy, pad)]


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
        help="Password (CalDAV); for Google / Microsoft accounts connected before OAuth refresh, their access "
        "token. Stored encrypted",
        groups="core.group_system",
    )
    refresh_token = fields.Char(
        help="OAuth refresh token (Google, Microsoft); stored encrypted", groups="core.group_system", copy=False
    )
    access_token = fields.Char(
        help="Current OAuth access token, refreshed automatically; stored encrypted",
        groups="core.group_system",
        copy=False,
    )
    token_expiry = fields.Datetime(readonly=True, copy=False, help="When the current access token expires")
    calendar_id = fields.Char(help="Remote calendar id (Google, Microsoft); default 'primary'")
    sync_token = fields.Char(readonly=True, copy=False)
    last_sync = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)
    state = fields.Selection(
        [("active", "Active"), ("error", "Error"), ("reconnect", "Needs reconnection"), ("paused", "Paused")],
        default="active",
        required=True,
    )
    link_ids = fields.One2many("calendar.sync_link", "account_id", "Synchronised events")

    @staticmethod
    def _encrypt_secrets(vals: dict[str, Any]) -> dict[str, Any]:
        for name in SECRET_FIELDS:
            if vals.get(name) and not crypto.is_encrypted(vals[name]):
                vals[name] = crypto.encrypt(vals[name])
        return vals

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        return super().create([self._encrypt_secrets(vals) for vals in batch])

    def write(self, vals):  # type: ignore[no-untyped-def]
        vals = self._encrypt_secrets(dict(vals))
        if "state" not in vals and (vals.get("refresh_token") or vals.get("secret")):
            # new credentials entered by a person: an account waiting for reconnection syncs again
            reconnect = self.filtered(lambda a: a.state == "reconnect")
            if reconnect:
                reconnect.write({**vals, "state": "active", "last_error": None})
                rest = self - reconnect
                return rest.write(vals) if rest else True
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
