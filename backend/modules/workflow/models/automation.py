"""Automated actions: trigger + optional filters + a list of actions written as data."""

from __future__ import annotations

import secrets
from typing import Any

from tessera.exceptions import ValidationError
from tessera.expr import ExpressionError, compile_expression
from tessera.orm import Model, api, fields
from tessera.orm.domain import Domain

TRIGGERS = [
    ("on_create", "On creation"),
    ("on_write", "On update"),
    ("on_create_or_write", "On creation or update"),
    ("on_delete", "On deletion"),
    ("on_time", "Based on a date field"),
    ("on_transition", "On workflow transition"),
    ("on_webhook", "On incoming webhook"),
]

ACTION_TYPES = {
    "update": {"values"},  # {"values": {"field": "<expression>"}}
    "post_message": {"body"},  # body is an expression; optional "internal"
    "schedule_activity": set(),  # optional activity_type, summary (expr), days, user (expr → id)
    "notify": {"title"},  # optional "users" expression → list of ids, default followers
    "create_record": {"model", "values"},
    "call_webhook": {"url"},  # POST the record as JSON (via a job)
    "call_method": {"method"},  # an @api.expose method of the record, without arguments
}


class Automation(Model):
    _name = "workflow.automation"
    _description = "Automated action"
    _order = "model_name, sequence, id"
    _audit = True
    _indexes = [("model_trigger", ["model_name", "trigger", "active"])]

    name = fields.Char(required=True)
    model_name = fields.Char("Model", required=True, index=True)
    trigger = fields.Selection(TRIGGERS, required=True, default="on_create_or_write")
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    filter_domain = fields.Json(help="Records must match this domain (after the change)")
    pre_filter_domain = fields.Json(help="On update: records must have matched this domain before the change")
    watched_fields = fields.Json(default=list, help="On update: only when one of these fields changes (empty = any)")
    condition = fields.Char(help="Extra expression, e.g. record.amount > 1000 and record.stage_id.name != 'Lost'")
    date_field = fields.Char(help="On time: the date/datetime field")
    offset_days = fields.Integer(default=0, help="On time: negative = before the date, positive = after")
    transition_id = fields.Many2one(
        "workflow.transition", ondelete="cascade", help="On transition: which one (empty = any)"
    )
    webhook_token = fields.Char(readonly=True, copy=False, index=True, groups="core.group_system")
    actions = fields.Json(default=list, help='[{"type": "update", "values": {"priority": "\'high\'"}}, …]')
    on_error = fields.Selection(
        [("log", "Log and continue"), ("raise", "Block the operation")], default="log", required=True
    )
    last_time_check = fields.Datetime(readonly=True, help="On time: trigger moments up to here have been processed")
    run_count = fields.Integer(readonly=True, default=0)
    run_ids = fields.One2many("workflow.automation_run", "automation_id", "Runs")

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("trigger") == "on_webhook" and not vals.get("webhook_token"):
                vals["webhook_token"] = secrets.token_urlsafe(24)
            if vals.get("trigger") == "on_time" and not vals.get("last_time_check"):
                vals["last_time_check"] = self.env.now()  # only trigger moments after activation
        return super().create(batch)

    @api.constrains("model_name", "trigger", "date_field", "actions", "filter_domain", "pre_filter_domain", "condition")
    def _check_definition(self) -> None:
        for auto in self:
            if auto.model_name not in self.env.registry.models:
                raise ValidationError(f"Unknown model '{auto.model_name}'", field="model_name")
            model = self.env[auto.model_name]
            if auto.trigger == "on_time":
                field = model._fields.get(auto.date_field or "")
                if field is None or field.type_name not in ("date", "datetime") or not field.store:
                    raise ValidationError(
                        "On time automations need a stored date or datetime field", field="date_field"
                    )
            for name in ("filter_domain", "pre_filter_domain"):
                if auto[name]:
                    try:
                        Domain(auto[name])
                    except Exception as exc:
                        raise ValidationError(f"Invalid domain: {exc}", field=name) from exc
            try:
                if auto.condition:
                    compile_expression(auto.condition)
                for index, action in enumerate(auto.actions or []):
                    _check_action(model, index, action)
            except ExpressionError as exc:
                raise ValidationError(str(exc), field="actions") from exc

    @api.expose
    def action_regenerate_token(self) -> bool:
        for auto in self:
            auto.write({"webhook_token": secrets.token_urlsafe(24)})
        return True

    @api.expose
    def action_run_on(self, res_ids: list[int]) -> dict[str, Any]:
        """Run the actions now on the given records (testing an automation from the UI)."""
        from modules.workflow.engine import run_automation

        self.ensure_one()
        records = self.env[self.model_name].browse(res_ids)
        records.check_access("write")
        done = run_automation(self, records.exists(), "manual")
        return {"ran": done}


def _check_action(model: Model, index: int, action: Any) -> None:
    if not isinstance(action, dict) or action.get("type") not in ACTION_TYPES:
        raise ValidationError(f"Action {index + 1}: unknown type (one of {', '.join(ACTION_TYPES)})", field="actions")
    missing = ACTION_TYPES[action["type"]] - set(action)
    if missing:
        raise ValidationError(f"Action {index + 1}: missing {', '.join(sorted(missing))}", field="actions")
    if action["type"] in ("update", "create_record"):
        target = model.env[action.get("model") or model._name] if action["type"] == "create_record" else model
        for fname, expr in (action.get("values") or {}).items():
            if fname not in target._fields:
                raise ValidationError(f"Action {index + 1}: unknown field '{fname}'", field="actions")
            compile_expression(str(expr))
    for key in ("body", "summary", "title", "user", "users"):
        if key in action and action[key] is not None:
            compile_expression(str(action[key]))
    if action["type"] == "call_method" and action["method"] not in type(model)._exposed_methods:
        raise ValidationError(f"Action {index + 1}: '{action['method']}' is not a callable method", field="actions")
    if action["type"] == "call_webhook" and not str(action["url"]).startswith(("https://", "http://")):
        raise ValidationError(f"Action {index + 1}: webhook URL must be http(s)", field="actions")


class AutomationRun(Model):
    _name = "workflow.automation_run"
    _description = "Automation run"
    _order = "id desc"
    _rec_name = "status"
    _studio = False
    _sql_constraints = [("once_key_uniq", "UNIQUE (once_key)", "This automation already ran for that moment")]

    automation_id = fields.Many2one("workflow.automation", required=True, ondelete="cascade", index=True)
    model = fields.Char(required=True)
    res_id = fields.Integer(index=True)
    event = fields.Char()
    status = fields.Selection([("done", "Done"), ("skipped", "Skipped"), ("error", "Error")], required=True)
    error = fields.Text()
    once_key = fields.Char(help="On time: automation:record:moment — guarantees a single run", index=True)
