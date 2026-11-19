from __future__ import annotations

from tessera.orm import Model, fields


class Language(Model):
    _name = "core.language"
    _description = "Language"
    _order = "name"
    _sql_constraints = [("code_uniq", "UNIQUE (code)", "Language codes must be unique")]

    name = fields.Char(required=True)
    code = fields.Char("Locale code", required=True, size=16, index=True, help="e.g. en_US, fr_FR")
    iso_code = fields.Char(size=8)
    active = fields.Boolean(default=False)
    direction = fields.Selection([("ltr", "Left to right"), ("rtl", "Right to left")], default="ltr")
    date_format = fields.Char(default="%m/%d/%Y")
    time_format = fields.Char(default="%H:%M:%S")
    decimal_point = fields.Char(default=".", size=1)
    thousands_sep = fields.Char(default=",", size=1)
    week_start = fields.Integer(default=1, help="1 = Monday … 7 = Sunday")

    def active_codes(self) -> list[str]:
        return [lang.code for lang in self.elevated().search([("active", "=", True)])]
