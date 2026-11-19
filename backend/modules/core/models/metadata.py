"""Introspectable model metadata (synced from the registry on every install/upgrade)."""

from __future__ import annotations

from tessera.orm import Model, fields


class MetaModel(Model):
    _name = "core.model"
    _description = "Model"
    _order = "name"
    _studio = False
    _sql_constraints = [("name_uniq", "UNIQUE (name)", "Model names are unique")]

    name = fields.Char("Technical name", required=True, index=True)
    description = fields.Char(required=True)
    table = fields.Char()
    module = fields.Char(index=True)
    is_abstract = fields.Boolean(default=False)
    studio = fields.Boolean("Customisable in Studio", default=True)
    log_access = fields.Boolean(default=True)
    soft_delete = fields.Boolean(default=False)
    rec_name = fields.Char()
    order = fields.Char()
    mixins = fields.Json(default=list)
    field_ids = fields.One2many("core.model_field", "model_id", "Fields")
    info = fields.Text()


class MetaField(Model):
    _name = "core.model_field"
    _description = "Model field"
    _order = "model_name, name"
    _studio = False
    _sql_constraints = [("model_field_uniq", "UNIQUE (model_name, name)", "Field names are unique per model")]

    model_id = fields.Many2one("core.model", required=True, ondelete="cascade")
    model_name = fields.Char(required=True, index=True)
    name = fields.Char(required=True, index=True)
    type = fields.Char(required=True)
    string = fields.Char("Label", required=True, translate=True)
    help = fields.Text(translate=True)
    required = fields.Boolean()
    readonly = fields.Boolean()
    store = fields.Boolean(default=True)
    compute = fields.Char()
    related = fields.Char()
    depends = fields.Json(default=list)
    translate = fields.Boolean()
    company_dependent = fields.Boolean()
    tracking = fields.Boolean()
    index = fields.Char()
    groups = fields.Json(default=list)
    comodel = fields.Char("Related model")
    selection = fields.Json()
    module = fields.Char(index=True)
    studio = fields.Boolean("Created in Studio", default=False)
    pii = fields.Boolean("Personal data", default=False)
    active = fields.Boolean(default=True)
