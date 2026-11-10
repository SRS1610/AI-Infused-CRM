"""In-app notifications (delivered over the realtime hub) and Web Push subscriptions."""

from __future__ import annotations

from typing import Any

from tessera.orm import Model, api, fields
from tessera.realtime import publish_after_commit


class Notification(Model):
    _name = "core.notification"
    _description = "Notification"
    _order = "id desc"
    _rec_name = "title"
    _studio = False
    _indexes = [("user_unread", ["user_id", "is_read"])]

    user_id = fields.Many2one("core.user", required=True, ondelete="cascade", index=True)
    title = fields.Char(required=True)
    body = fields.Text()
    kind = fields.Selection(
        [
            ("message", "Message"),
            ("mention", "Mention"),
            ("activity", "Activity"),
            ("system", "System"),
            ("job", "Job"),
        ],
        default="system",
        required=True,
    )
    model = fields.Char()
    res_id = fields.Integer()
    url = fields.Char()
    is_read = fields.Boolean(default=False)
    read_at = fields.Datetime()

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        records = super().create(vals_list)
        for note in records.elevated():
            publish_after_commit(
                self.env.cr,
                f"user:{note.user_id.ids[0]}",
                "notification",
                {
                    "id": note.ids[0],
                    "title": note.title,
                    "body": note.body,
                    "kind": note.kind,
                    "model": note.model,
                    "res_id": note.res_id,
                },
            )
        return records

    @api.expose
    def action_mark_read(self) -> bool:
        self.filtered(lambda n: not n.is_read).write({"is_read": True, "read_at": self.env.now()})
        return True

    @api.model
    def notify(self, user_ids: list[int], title: str, body: str = "", **extra: Any) -> Model:
        return self.elevated("notify").create(
            [{"user_id": uid, "title": title, "body": body, **extra} for uid in dict.fromkeys(user_ids)]
        )


class PushSubscription(Model):
    _name = "core.push_subscription"
    _description = "Web Push subscription"
    _rec_name = "endpoint"
    _studio = False

    user_id = fields.Many2one("core.user", required=True, ondelete="cascade", default=lambda self: self.env.uid)
    endpoint = fields.Char(required=True, index="unique")
    p256dh = fields.Char(required=True)
    auth = fields.Char(required=True)
    user_agent = fields.Char()
