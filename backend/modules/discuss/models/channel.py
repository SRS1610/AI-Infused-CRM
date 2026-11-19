"""Channels and memberships. Messages are chatter messages with ``model = 'discuss.channel'``, so
mentions, reactions, attachments and threads share the record-chatter code path."""

from __future__ import annotations

from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class Channel(Model):
    _name = "discuss.channel"
    _description = "Discussion channel"
    _order = "channel_type, name, id"
    _inherit = ["chatter.thread"]
    _track_create = False
    _follower_notifications = False
    _sql_constraints = [("uuid_uniq", "UNIQUE (uuid)", "A direct conversation between two people exists only once")]

    name = fields.Char(required=True, tracking=True)
    description = fields.Text()
    channel_type = fields.Selection(
        [("channel", "Channel"), ("group", "Group chat"), ("dm", "Direct message")], default="channel", required=True
    )
    privacy = fields.Selection(
        [("public", "Everyone"), ("groups", "Selected groups"), ("invite", "Invited members only")],
        default="public",
        required=True,
    )
    group_ids = fields.Many2many(
        "core.group", relation="discuss_channel__group_ids", column1="channel_id", column2="group_id"
    )
    member_ids = fields.One2many("discuss.member", "channel_id", "Members")
    member_count = fields.Integer(compute="_compute_member_count")
    uuid = fields.Char(index=True, copy=False, help="Stable key of a direct conversation")
    last_message_at = fields.Datetime(readonly=True, index=True)
    company_id = fields.Many2one("core.company", ondelete="cascade")
    active = fields.Boolean(default=True)

    def _compute_member_count(self) -> None:
        counts: dict[int, int] = {}
        if self.ids:
            rows = self.env.cr.fetchall(
                "SELECT channel_id, COUNT(*) FROM discuss_member WHERE channel_id = ANY(:ids) GROUP BY channel_id",
                {"ids": list(self.ids)},
            )
            counts = {r[0]: r[1] for r in rows}
        for ch in self:
            ch.member_count = counts.get(ch.ids[0], 0) if ch.ids else 0

    @api.constrains("channel_type", "privacy")
    def _check_type(self) -> None:
        for ch in self:
            if ch.channel_type in ("dm", "group") and ch.privacy != "invite":
                raise ValidationError(_("Direct messages and group chats are invite-only"), field="privacy")

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        channels = super().create(vals_list)
        members = self.env["discuss.member"].elevated("channel creator")
        for ch in channels:  # the creator joins
            if self.env.uid > 1 and not ch.elevated().member_ids.filtered(lambda m: m.user_id.ids == [self.env.uid]):
                members.create({"channel_id": ch.ids[0], "user_id": self.env.uid})
        return channels

    def _is_member(self, uid: int | None = None) -> bool:
        uid = uid or self.env.uid
        return bool(
            self.env["discuss.member"]
            .elevated()
            .search_count([("channel_id", "=", self.ids[0]), ("user_id", "=", uid)])
        )

    @api.expose
    def action_join(self) -> bool:
        for ch in self:
            ch.check_access("read")
            if ch.privacy == "invite" and not ch._is_member():
                raise UserError(_("This channel is invite-only"))
            if not ch._is_member():
                self.env["discuss.member"].elevated("join").create({"channel_id": ch.ids[0], "user_id": self.env.uid})
        return True

    @api.expose
    def action_leave(self) -> bool:
        members = self.env["discuss.member"].elevated("leave")
        members.search([("channel_id", "in", self.ids), ("user_id", "=", self.env.uid)]).unlink()
        return True

    @api.expose
    def action_invite(self, user_ids: list[int]) -> bool:
        self.ensure_one()
        if not self._is_member() and not self.env.registry.security.has_groups(self.env, ["core.group_system"]):
            raise UserError(_("Only members can invite people"))
        if self.channel_type == "dm":
            raise UserError(_("Start a group chat to talk with more people"))
        members = self.env["discuss.member"].elevated("invite")
        existing = set(members.search([("channel_id", "=", self.ids[0])]).mapped("user_id").ids)
        for uid in user_ids:
            if uid not in existing:
                members.create({"channel_id": self.ids[0], "user_id": uid})
        return True

    @api.model
    def get_or_create_dm(self, user_id: int) -> Model:
        if user_id == self.env.uid:
            raise UserError(_("Pick someone else"))
        pair = sorted([self.env.uid, int(user_id)])
        key = f"dm:{pair[0]}:{pair[1]}"
        channels = self.elevated("direct message").with_context(active_test=False)
        found = channels.search([("uuid", "=", key)], limit=1)
        if found:
            return found.with_env(self.env)
        other = self.env["core.user"].elevated().browse([user_id])
        ch = channels.create(
            {
                "name": f"{self.env.user.elevated().name}, {other.name}",
                "channel_type": "dm",
                "privacy": "invite",
                "uuid": key,
            }
        )
        members = self.env["discuss.member"].elevated("direct message")
        for uid in pair:
            if not members.search_count([("channel_id", "=", ch.ids[0]), ("user_id", "=", uid)]):
                members.create({"channel_id": ch.ids[0], "user_id": uid, "notify": "all"})
        return ch.with_env(self.env)


class Member(Model):
    _name = "discuss.member"
    _description = "Channel member"
    _rec_name = "user_id"
    _studio = False
    _sql_constraints = [("member_uniq", "UNIQUE (channel_id, user_id)", "Already a member")]

    channel_id = fields.Many2one("discuss.channel", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one("core.user", required=True, ondelete="cascade", index=True)
    last_seen_message_id = fields.Integer(default=0)
    notify = fields.Selection(
        [("all", "Every message"), ("mentions", "Mentions only"), ("none", "Nothing")],
        default="mentions",
        required=True,
    )
    is_pinned = fields.Boolean(default=True)
    joined_at = fields.Datetime(default=lambda self: self.env.now())


def unread_counts(env: Any, uid: int, channel_ids: list[int]) -> dict[int, int]:
    if not channel_ids:
        return {}
    rows = env.cr.fetchall(
        """SELECT m.channel_id, COUNT(msg.id) FROM discuss_member m
             LEFT JOIN chatter_message msg ON msg.model = 'discuss.channel' AND msg.res_id = m.channel_id
                  AND msg.id > COALESCE(m.last_seen_message_id, 0) AND COALESCE(msg.author_id, 0) <> :uid
                  AND msg.message_type IN ('comment', 'email')
            WHERE m.user_id = :uid AND m.channel_id = ANY(:ids)
         GROUP BY m.channel_id""",
        {"uid": uid, "ids": channel_ids},
    )
    return {r[0]: int(r[1]) for r in rows}
