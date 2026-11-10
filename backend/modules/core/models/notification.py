"""In-app notifications (delivered over the realtime hub) and Web Push subscriptions."""

from __future__ import annotations

from typing import Any

from tessera.i18n import Message
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
        vals_list = _localize_values(self.env, vals_list)
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
        """Notify users; a ``_()`` title or body is written in each recipient's language."""
        return self.elevated("notify").create(
            [{"user_id": uid, "title": title, "body": body, **extra} for uid in dict.fromkeys(user_ids)]
        )


def _localize_values(env: Any, vals_list: Any) -> Any:
    """Render ``_()`` titles and bodies in the language of each notification's recipient."""
    batch = vals_list if isinstance(vals_list, list) else [vals_list]
    pending = [v for v in batch if isinstance(v.get("title"), Message) or isinstance(v.get("body"), Message)]
    if not pending:
        return vals_list
    uids = {int(v["user_id"]) for v in pending if v.get("user_id")}
    users = env["core.user"].elevated().with_context(active_test=False).browse(sorted(uids)).exists()
    langs = {user.ids[0]: user.lang for user in users}
    modules = env.registry.module_order
    out = []
    for vals in batch:
        lang = langs.get(int(vals.get("user_id") or 0))
        out.append(
            {
                key: value.in_language(lang, modules=modules) if isinstance(value, Message) else value
                for key, value in vals.items()
            }
        )
    return out if isinstance(vals_list, list) else out[0]


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
