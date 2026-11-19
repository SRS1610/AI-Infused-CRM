"""Studio records: custom fields, the change log (with before/after metadata) and snapshots.

Designers never write these directly: every change goes through ``modules.studio.service`` so the
schema, the registry and the change log move together."""

from __future__ import annotations

from tessera.orm import Model, fields

FIELD_TYPES = [
    ("char", "Text"),
    ("text", "Long text"),
    ("html", "Rich text"),
    ("integer", "Whole number"),
    ("float", "Decimal number"),
    ("monetary", "Amount"),
    ("boolean", "Checkbox"),
    ("date", "Date"),
    ("datetime", "Date and time"),
    ("selection", "Choice"),
    ("many2one", "Link to one record"),
    ("many2many", "Links to several records"),
]
CHANGE_KINDS = [
    ("add_field", "Field added"),
    ("edit_field", "Field changed"),
    ("view_change", "View changed"),
    ("new_view", "View created"),
    ("automation", "Automated action"),
    ("workflow", "Workflow"),
]


class StudioField(Model):
    _name = "studio.field"
    _description = "Studio field"
    _order = "model_name, id"
    _studio = False

    model_name = fields.Char(required=True, index=True)
    name = fields.Char(required=True, help="Technical name, always starting with x_")
    ttype = fields.Selection(FIELD_TYPES, "Type", required=True)
    label = fields.Char(required=True)
    help = fields.Text()
    required = fields.Boolean()
    readonly = fields.Boolean()
    required_if = fields.Json(help="Domain: required when the record matches")
    readonly_if = fields.Json(help="Domain: read-only when the record matches")
    default = fields.Json(help="Default value")
    selection = fields.Json(help="[[value, label], …] for choice fields")
    comodel = fields.Char(help="Target model of link fields")
    currency_field = fields.Char(help="Currency field of amount fields")
    groups = fields.Json(default=list, help="External ids of the groups that may see the field")
    widget = fields.Char(help="Client widget used where Studio places the field")
    tracking = fields.Boolean()
    active = fields.Boolean(default=True)

    _sql_constraints = [
        ("model_name_unique", "UNIQUE (model_name, name)", "A Studio field with this name already exists")
    ]


class StudioChange(Model):
    _name = "studio.change"
    _description = "Studio change"
    _order = "id desc"
    _studio = False

    kind = fields.Selection(CHANGE_KINDS, required=True)
    model_name = fields.Char(index=True)
    summary = fields.Char(required=True)
    before = fields.Json(help="Metadata before the change")
    after = fields.Json(help="Metadata after the change")
    res_model = fields.Char(help="Record created by the change (studio.field, web.view, workflow.automation…)")
    res_id = fields.Integer()
    company_id = fields.Many2one("core.company", help="Set when the change applies to one company only")
    state = fields.Selection([("applied", "Applied"), ("rolled_back", "Rolled back")], default="applied", required=True)
    rolled_back_at = fields.Datetime()
    rolled_back_by = fields.Many2one("core.user")


class StudioSnapshot(Model):
    _name = "studio.snapshot"
    _description = "Studio snapshot"
    _order = "id desc"
    _studio = False

    name = fields.Char(required=True)
    last_change_id = fields.Integer(help="Every change after this one is undone when rolling back to the snapshot")
