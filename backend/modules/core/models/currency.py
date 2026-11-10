from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, fields


class Currency(Model):
    _name = "core.currency"
    _description = "Currency"
    _order = "name"
    _sql_constraints = [("code_uniq", "UNIQUE (name)", "Currency codes must be unique")]

    name = fields.Char("ISO code", required=True, size=3, index=True)
    full_name = fields.Char("Name", translate=True)
    symbol = fields.Char(required=True, size=8)
    decimal_places = fields.Integer(default=2)
    rounding = fields.Float(default=0.01, digits=(16, 6))
    position = fields.Selection([("before", "Before amount"), ("after", "After amount")], default="before")
    active = fields.Boolean(default=True)
    rate_ids = fields.One2many("core.currency_rate", "currency_id", "Rates")

    def round(self, amount: float) -> float:
        self.ensure_one()
        places = int(self.decimal_places or 0)
        quant = Decimal(1).scaleb(-places)
        return float(Decimal(str(amount)).quantize(quant, rounding=ROUND_HALF_UP))

    def _rate_on(self, company: Model | None, date: dt.date | None) -> float:
        """Units of this currency per one unit of the company currency on ``date``."""
        self.ensure_one()
        company = company or self.env.company
        if company and company.currency_id.ids == self.ids:
            return 1.0
        date = date or self.env.today()
        rates = self.env["core.currency_rate"].elevated()
        domain = [("currency_id", "=", self.ids[0]), ("date", "<=", date)]
        if company:
            rate = rates.search([*domain, ("company_id", "=", company.ids[0])], order="date desc", limit=1)
            if rate:
                return float(rate.rate)
        rate = rates.search([*domain, ("company_id", "is_not_set")], order="date desc", limit=1)
        if not rate:
            raise UserError(_("No exchange rate for {name} on {date}", name=self.name, date=date), currency=self.name)
        return float(rate.rate)

    def convert(
        self, amount: float, to_currency: Model, company: Model | None = None, date: dt.date | None = None
    ) -> float:
        self.ensure_one()
        to_currency.ensure_one()
        if self.ids == to_currency.ids:
            return float(to_currency.round(amount))
        rate_from = self._rate_on(company, date)
        rate_to = to_currency._rate_on(company, date)
        return float(to_currency.round(amount / rate_from * rate_to))

    def format(self, amount: float, lang: Model | None = None) -> str:
        self.ensure_one()
        places = int(self.decimal_places or 0)
        text = f"{amount:,.{places}f}"
        return f"{self.symbol}{text}" if self.position == "before" else f"{text} {self.symbol}"


class CurrencyRate(Model):
    _name = "core.currency_rate"
    _description = "Currency rate"
    _order = "date desc, id desc"
    _rec_name = "date"
    _sql_constraints = [
        (
            "unique_per_day",
            "UNIQUE (currency_id, company_id, date)",
            "Only one rate per currency, company and day",
        )
    ]

    currency_id = fields.Many2one("core.currency", required=True, ondelete="cascade")
    company_id = fields.Many2one("core.company", ondelete="cascade", help="Empty = applies to every company")
    date = fields.Date(required=True, default=lambda self: self.env.today(), index=True)
    rate = fields.Float(
        required=True, digits=(16, 6), help="Units of the currency per one unit of the company currency"
    )
