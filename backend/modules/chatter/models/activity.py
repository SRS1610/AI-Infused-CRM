"""Scheduled activities (call, meeting, email, to-do, …)."""

from __future__ import annotations

import datetime as dt

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class ActivityType(Model):
    _name = "chatter.activity_type"
    _description = "Activity type"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    category = fields.Selection(
        [
            ("todo", "To-do"),
            ("call", "Phone call"),
            ("meeting", "Meeting"),
            ("email", "Email"),
            ("upload", "Upload document"),
        ],
        default="todo",
        required=True,
    )
    icon = fields.Char(default="check")
    default_days = fields.Integer(default=0, help="Default deadline: today + N days")
    default_summary = fields.Char(translate=True)
    model_name = fields.Char(help="Empty = available on every model")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)


class Activity(Model):
    _name = "chatter.activity"
    _description = "Activity"
    _order = "date_deadline, id"
    _rec_name = "summary"
    _indexes = [("model_res_id", ["model", "res_id"]), ("user_deadline", ["user_id", "date_deadline"])]

    model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True)
    res_name = fields.Char("Record")
    activity_type_id = fields.Many2one("chatter.activity_type", required=True, ondelete="restrict")
    category = fields.Selection(related="activity_type_id.category")
    summary = fields.Char()
    note = fields.Html()
    date_deadline = fields.Date("Due date", required=True, index=True, default=lambda self: self.env.today())
    user_id = fields.Many2one(
        "core.user", "Assigned to", required=True, ondelete="cascade", default=lambda self: self.env.uid, index=True
    )
    state = fields.Selection(
        [("overdue", "Overdue"), ("today", "Today"), ("planned", "Planned"), ("done", "Done")],
        compute="_compute_state",
        search="_search_state",
    )
    done = fields.Boolean(default=False, index=True)
    done_at = fields.Datetime()
    feedback = fields.Text()
    company_id = fields.Many2one(
        "core.company", ondelete="set_null", default=lambda self: self.env.company.ids[0] if self.env.company else None
    )

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        records = super().create(vals_list)
        records._invalidate_record_state()
        return records

    def write(self, vals):  # type: ignore[no-untyped-def]
        result = super().write(vals)
        self._invalidate_record_state()
        return result

    def unlink(self) -> bool:
        targets = [(a.model, a.res_id) for a in self.elevated()]
        result = super().unlink()
        self._invalidate_record_state(targets)
        return result

    def _invalidate_record_state(self, targets: list[tuple[str, int]] | None = None) -> None:
        """The thread's activity_state is computed from activities: drop its cached value."""
        pairs = targets if targets is not None else [(a.model, a.res_id) for a in self.elevated()]
        for model, res_id in pairs:
            if model in self.env.registry.models:
                self.env.cache.invalidate(model, ["activity_state", "activity_date_deadline"], [res_id])

    @api.depends("date_deadline", "done")
    def _compute_state(self) -> None:
        today = self.env.today()  # in the viewing user's timezone
        for act in self:
            if act.done:
                act.state = "done"
            elif act.date_deadline < today:
                act.state = "overdue"
            elif act.date_deadline == today:
                act.state = "today"
            else:
                act.state = "planned"

    def _search_state(self, operator: str, value: object) -> list[object]:
        today = self.env.today()
        values = value if isinstance(value, list | tuple) else [value]
        parts: list[object] = []
        for v in values:
            if v == "done":
                parts.append(["done", "=", True])
            elif v == "overdue":
                parts.append({"all": [["done", "=", False], ["date_deadline", "<", today.isoformat()]]})
            elif v == "today":
                parts.append({"all": [["done", "=", False], ["date_deadline", "=", today.isoformat()]]})
            elif v == "planned":
                parts.append({"all": [["done", "=", False], ["date_deadline", ">", today.isoformat()]]})
        node: object = {"any": parts} if parts else {"any": []}
        return [{"not": node}] if operator in ("!=", "not_in") else [node]

    @api.expose
    def action_done(self, feedback: str | None = None) -> bool:
        """Mark done and log it in the record's thread."""
        from modules.chatter.thread import post_message

        for act in self:
            if act.done:
                raise UserError(_("This activity is already done"))
            body = f"<p><b>{act.activity_type_id.name}</b> done: {act.summary or ''}</p>"
            if feedback:
                body += f"<p>{feedback}</p>"
            post_message(self.env, act.model, act.res_id, body, message_type="activity_done", notify=False)
            act.write({"done": True, "done_at": self.env.now(), "feedback": feedback})
        return True

    @api.expose
    def action_snooze(self, days: int = 1) -> bool:
        for act in self:
            act.write({"date_deadline": act.date_deadline + dt.timedelta(days=int(days))})
        return True
