from __future__ import annotations

from typing import Any

from tessera.exceptions import ValidationError
from tessera.orm import Model, api, fields


class Company(Model):
    _name = "core.company"
    _description = "Company"
    _order = "sequence, name"
    _parent_name = "parent_id"
    _audit = True

    name = fields.Char(required=True, index=True)
    parent_id = fields.Many2one("core.company", "Parent company", ondelete="restrict")
    child_ids = fields.One2many("core.company", "parent_id", "Branches")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    currency_id = fields.Many2one("core.currency", "Currency", required=True, ondelete="restrict")
    country_id = fields.Many2one("core.country", "Country", ondelete="set_null")
    state_id = fields.Many2one("core.country_state", "State", ondelete="set_null")
    street = fields.Char()
    street2 = fields.Char()
    city = fields.Char()
    zip = fields.Char()
    phone = fields.Char()
    email = fields.Char()
    website = fields.Char()
    vat = fields.Char("Tax ID")
    logo = fields.Image(attachment=False)
    primary_color = fields.Char(default="#1f4b99")
    report_header = fields.Html(sanitize=True)
    report_footer = fields.Html(sanitize=True)
    setting_ids = fields.One2many("core.company_setting", "company_id", "Settings")
    user_ids = fields.Many2many(
        "core.user", relation="core_user__company_ids", column1="core_company_id", column2="core_user_id"
    )

    @api.constrains("parent_id")
    def _check_parent(self) -> None:
        for company in self:
            seen = set()
            current = company
            while current:
                if current.ids[0] in seen:
                    raise ValidationError("A company cannot be its own ancestor")
                seen.add(current.ids[0])
                current = current.parent_id

    def get_setting(self, key: str, default: Any = None) -> Any:
        self.ensure_one()
        setting = (
            self.env["core.company_setting"]
            .elevated()
            .search([("company_id", "=", self.ids[0]), ("key", "=", key)], limit=1)
        )
        return setting.value if setting else default

    def set_setting(self, key: str, value: Any) -> None:
        self.ensure_one()
        settings = self.env["core.company_setting"]
        existing = settings.search([("company_id", "=", self.ids[0]), ("key", "=", key)], limit=1)
        if existing:
            existing.write({"value": value})
        else:
            settings.create({"company_id": self.ids[0], "key": key, "value": value})


class CompanySetting(Model):
    _name = "core.company_setting"
    _description = "Company setting"
    _rec_name = "key"
    _sql_constraints = [("company_key_uniq", "UNIQUE (company_id, key)", "A setting key must be unique per company")]

    company_id = fields.Many2one("core.company", required=True, ondelete="cascade")
    key = fields.Char(required=True, index=True)
    value = fields.Json()
    description = fields.Char()
