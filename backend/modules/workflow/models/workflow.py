"""Workflow definitions: states and transitions over a selection (or many2one) state field."""

from __future__ import annotations

from typing import Any

from tessera.exceptions import AccessError, ConflictError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields
from tessera.orm.domain import Domain


class Definition(Model):
    _name = "workflow.definition"
    _description = "Workflow"
    _order = "model_name, name"
    _audit = True

    name = fields.Char(required=True, translate=True)
    model_name = fields.Char("Model", required=True, index=True)
    state_field = fields.Char("State field", required=True, default="state")
    strict = fields.Boolean(
        default=True, help="State changes must go through a transition (direct writes to the state field are refused)"
    )
    active = fields.Boolean(default=True)
    state_ids = fields.One2many("workflow.state", "definition_id", "States")
    transition_ids = fields.One2many("workflow.transition", "definition_id", "Transitions")
    description = fields.Text()

    @api.constrains("model_name", "state_field", "active")
    def _check_target(self) -> None:
        for wf in self:
            if wf.model_name not in self.env.registry.models:
                raise ValidationError(_("Unknown model '{model_name}'", model_name=wf.model_name), field="model_name")
            field = self.env[wf.model_name]._fields.get(wf.state_field)
            if field is None or field.type_name not in ("selection", "char", "many2one"):
                raise ValidationError(
                    _("The state field must be a selection, char or many2one field"), field="state_field"
                )
            if wf.active:
                others = self.search(
                    [("model_name", "=", wf.model_name), ("active", "=", True), ("id", "!=", wf.ids[0])]
                )
                if others:
                    raise ValidationError(
                        _("'{model_name}' already has an active workflow", model_name=wf.model_name), field="model_name"
                    )

    def state_key(self, record: Model) -> str | None:
        value = record[self.state_field]
        if hasattr(value, "_ids"):
            return str(value.ids[0]) if value else None
        return value or None

    def state_value(self, state: Model) -> Any:
        field = self.env[self.model_name]._fields[self.state_field]
        return int(state.key) if field.type_name == "many2one" else state.key


class State(Model):
    _name = "workflow.state"
    _description = "Workflow state"
    _order = "definition_id, sequence, id"
    _sql_constraints = [("key_uniq", "UNIQUE (definition_id, key)", "State keys are unique per workflow")]

    definition_id = fields.Many2one("workflow.definition", required=True, ondelete="cascade", index=True)
    key = fields.Char(required=True, help="Value of the state field (selection key, or id for a many2one)")
    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    fold = fields.Boolean(help="Collapsed in the status bar unless current")
    is_final = fields.Boolean()


class Transition(Model):
    _name = "workflow.transition"
    _description = "Workflow transition"
    _order = "definition_id, sequence, id"

    definition_id = fields.Many2one("workflow.definition", required=True, ondelete="cascade", index=True)
    name = fields.Char(required=True, translate=True, help="Button label")
    from_state_ids = fields.Many2many(
        "workflow.state",
        relation="workflow_transition__from",
        column1="transition_id",
        column2="state_id",
        help="Empty = from any state",
    )
    to_state_id = fields.Many2one("workflow.state", required=True, ondelete="cascade")
    guard_domain = fields.Json(help="The record must match this domain for the transition to be available")
    guard_message = fields.Char(translate=True, help="Shown when the guard blocks the transition")
    group_ids = fields.Many2many(
        "core.group",
        relation="workflow_transition__groups",
        column1="transition_id",
        column2="group_id",
        help="Empty = everyone who can write the record",
    )
    confirm = fields.Char(translate=True, help="Ask for confirmation with this text")
    sequence = fields.Integer(default=10)
    highlight = fields.Boolean(help="Primary button")

    @api.constrains("guard_domain")
    def _check_guard(self) -> None:
        for tr in self:
            if tr.guard_domain:
                try:
                    Domain(tr.guard_domain)
                except Exception as exc:
                    raise ValidationError(_("Invalid guard domain: {exc}", exc=exc), field="guard_domain") from exc

    # -- evaluation ---------------------------------------------------------------------------------

    def blocked_reason(self, record: Model) -> str | None:
        """Why this transition cannot be taken on ``record`` by the current user (None = allowed)."""
        self.ensure_one()
        wf = self.definition_id
        current = wf.state_key(record)
        if self.from_state_ids and current not in self.from_state_ids.mapped("key"):
            return _("Not available from the current state")
        if current == self.to_state_id.key:
            return _("Already in that state")
        if self.group_ids:
            xmlids = [x for x in self.group_ids.get_external_id().values() if x]
            if not self.env.registry.security.has_groups(record.env, xmlids):
                return _("You are not allowed to do this")
        if self.guard_domain:
            matches = record.with_context(active_test=False).search(
                [("id", "=", record.ids[0]), Domain(self.guard_domain).node]
            )
            if not matches:
                return self.guard_message or _("Conditions for this step are not met")
        return None

    def apply(self, record: Model) -> Model:
        """Take the transition (checks everything, writes the state, runs on_transition automations)."""
        self.ensure_one()
        record.ensure_one()
        record.check_access("write")
        reason = self.blocked_reason(record)
        if reason is not None:
            if getattr(reason, "msgid", reason) == "You are not allowed to do this":
                raise AccessError(reason)
            raise ConflictError(reason, transition=self.ids[0])
        wf = self.definition_id
        old = wf.state_key(record)
        record.with_context(workflow_transition=self.ids[0]).write({wf.state_field: wf.state_value(self.to_state_id)})
        from modules.workflow.engine import run_transition_automations

        run_transition_automations(record, self, old)
        return record


def workflow_for(env: Any, model_name: str) -> Model:
    return (
        env["workflow.definition"].elevated().search([("model_name", "=", model_name), ("active", "=", True)], limit=1)
    )
