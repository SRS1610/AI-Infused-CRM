from __future__ import annotations

from tessera.orm import Model, fields


class Country(Model):
    _name = "core.country"
    _description = "Country"
    _order = "name"
    _sql_constraints = [("code_uniq", "UNIQUE (code)", "Country codes must be unique")]

    name = fields.Char(required=True, translate=True, index=True)
    code = fields.Char("ISO code", required=True, size=2, index=True)
    phone_code = fields.Char("Dialing code")
    currency_id = fields.Many2one("core.currency", ondelete="set_null")
    active = fields.Boolean(default=True)
    state_ids = fields.One2many("core.country_state", "country_id", "States")
    vat_label = fields.Char(default="VAT")
    address_format = fields.Text(default="%(street)s\n%(street2)s\n%(city)s %(state_code)s %(zip)s\n%(country_name)s")


class CountryState(Model):
    _name = "core.country_state"
    _description = "Country state / province"
    _order = "country_id, name"
    _sql_constraints = [
        ("code_per_country_uniq", "UNIQUE (country_id, code)", "State codes must be unique per country")
    ]

    country_id = fields.Many2one("core.country", required=True, ondelete="cascade")
    name = fields.Char(required=True)
    code = fields.Char(required=True, size=8)
