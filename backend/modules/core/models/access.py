from __future__ import annotations

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields
from tessera.orm.domain import parse


class ModelAccess(Model):
    """Model-level CRUD rights per group. A row without a group applies to every user."""

    _name = "core.model_access"
    _description = "Access right"
    _order = "model_name, name"

    name = fields.Char(required=True)
    model_name = fields.Char("Model", required=True, index=True)
    group_id = fields.Many2one("core.group", "Group", ondelete="cascade", help="Empty = every authenticated user")
    perm_read = fields.Boolean("Read", default=True)
    perm_write = fields.Boolean("Write", default=False)
    perm_create = fields.Boolean("Create", default=False)
    perm_unlink = fields.Boolean("Delete", default=False)
    active = fields.Boolean(default=True)

    @api.constrains("model_name")
    def _check_model(self) -> None:
        for access in self:
            if access.model_name not in self.env.registry.models:
                raise ValidationError(
                    _("Unknown model '{model_name}'", model_name=access.model_name), field="model_name"
                )


class RecordRule(Model):
    """Row-level rules: global rules are AND-ed, group rules OR-ed, then AND-ed together."""

    _name = "core.record_rule"
    _description = "Record rule"
    _order = "model_name, name"

    name = fields.Char(required=True)
    model_name = fields.Char("Model", required=True, index=True)
    domain = fields.Json(required=True, help="Nested-JSON domain; may use $uid, $company, $companies, $user.<field>")
    group_ids = fields.Many2many(
        "core.group",
        "Groups",
        relation="core_record_rule__group_ids",
        column1="core_record_rule_id",
        column2="core_group_id",
    )
    is_global = fields.Boolean(compute="_compute_is_global", store=True)
    perm_read = fields.Boolean("Apply to read", default=True)
    perm_write = fields.Boolean("Apply to write", default=True)
    perm_create = fields.Boolean("Apply to create", default=True)
    perm_unlink = fields.Boolean("Apply to delete", default=True)
    active = fields.Boolean(default=True)

    @api.depends("group_ids")
    def _compute_is_global(self) -> None:
        for rule in self:
            rule.is_global = not rule.group_ids

    @api.constrains("model_name", "domain")
    def _check_domain(self) -> None:
        for rule in self:
            if rule.model_name not in self.env.registry.models:
                raise ValidationError(_("Unknown model '{model_name}'", model_name=rule.model_name), field="model_name")
            parse(rule.domain)


class RegistryStamp(Model):
    """One row whose ``version`` changes when the model structure changes at runtime (Studio fields);
    every process compares it with the version its registry was built for and rebuilds when it differs."""

    _name = "core.registry_stamp"
    _description = "Registry stamp"
    _log_access = False
    _studio = False

    version = fields.Integer()


class SecurityStamp(Model):
    """One row whose ``version`` changes (randomly) in the same transaction as any change to groups,
    access rights, record rules or memberships. Every process compares it with the version of its
    cached rules, so changes made by other workers — and rolled-back changes — are never served stale."""

    _name = "core.security_stamp"
    _description = "Security cache stamp"
    _log_access = False
    _studio = False

    version = fields.Integer()
