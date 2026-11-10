"""Payment terms: installments, due-date rules and early-payment discounts (ERD_PHASE_2 §3.5)."""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from modules.account.models.tax import _round
from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


def _end_of_month(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


class PaymentTerm(Model):
    _name = "account.payment_term"
    _description = "Payment terms"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    note = fields.Html("Description on documents", translate=True)
    sequence = fields.Integer(default=10)
    line_ids = fields.One2many("account.payment_term_line", "payment_id", "Installments", copy=True)
    early_discount = fields.Boolean("Early-payment discount")
    discount_percentage = fields.Float("Discount %", digits=(5, 2), default=2.0)
    discount_days = fields.Integer("Paid within (days)", default=10)
    early_pay_discount_computation = fields.Selection(
        [("included", "On early payment (taxes adjusted)"), ("excluded", "Never (taxes unchanged)")],
        "Tax treatment",
        default="included",
        required=True,
    )
    company_id = fields.Many2one("core.company", ondelete="cascade", help="Empty = shared by every company")
    active = fields.Boolean(default=True)

    @api.constrains("line_ids", "early_discount", "discount_percentage")
    def _check_lines(self) -> None:
        for term in self:
            lines = term.line_ids.sorted(lambda r: (r.sequence or 0, r.ids[0]))
            balances = lines.filtered(lambda r: r.value == "balance")
            if lines and (len(balances) != 1 or lines[len(lines) - 1].ids != balances.ids):
                raise ValidationError(_("Payment terms need exactly one 'balance' installment, last"), field="line_ids")
            if sum(lines.filtered(lambda r: r.value == "percent").mapped("value_amount")) > 100:
                raise ValidationError(_("Installment percentages exceed 100 %"), field="line_ids")
            if term.early_discount and not 0 < (term.discount_percentage or 0) < 100:
                raise ValidationError(_("The discount must be between 0 and 100 %"), field="discount_percentage")

    def compute_installments(
        self, date_ref: dt.date, total: float, total_company: float, currency: Model, company_currency: Model
    ) -> list[dict[str, Any]]:
        """``[{date, amount, amount_company}]`` summing exactly to the totals (signs preserved).

        With no installment lines the whole amount is due on ``date_ref``.
        """
        places = int(currency.decimal_places or 2)
        cplaces = int(company_currency.decimal_places or 2)
        if not self or not self.line_ids:
            return [{"date": date_ref, "amount": total, "amount_company": total_company}]
        out: list[dict[str, Any]] = []
        left, left_company = total, total_company
        for line in self.line_ids.sorted(lambda r: (r.sequence or 0, r.ids[0])):
            due = line.due_date(date_ref)
            if line.value == "balance":
                amount, amount_company = left, left_company
            elif line.value == "percent":
                amount = _round(total * float(line.value_amount or 0) / 100, places)
                amount_company = _round(total_company * float(line.value_amount or 0) / 100, cplaces)
            else:  # fixed, in document currency, never more than what is left
                sign = 1 if total >= 0 else -1
                amount = sign * min(abs(float(line.value_amount or 0)), abs(left))
                amount_company = _round(total_company * (amount / total), cplaces) if total else 0.0
            left = _round(left - amount, places)
            left_company = _round(left_company - amount_company, cplaces)
            if amount or amount_company or line.value == "balance":
                out.append({"date": due, "amount": amount, "amount_company": amount_company})
        # merge installments falling on the same day
        merged: dict[dt.date, dict[str, Any]] = {}
        for row in out:
            slot = merged.setdefault(row["date"], {"date": row["date"], "amount": 0.0, "amount_company": 0.0})
            slot["amount"] = _round(slot["amount"] + row["amount"], places)
            slot["amount_company"] = _round(slot["amount_company"] + row["amount_company"], cplaces)
        return sorted(merged.values(), key=lambda r: r["date"])

    def discount_date(self, date_ref: dt.date) -> dt.date | None:
        self.ensure_one()
        return date_ref + dt.timedelta(days=int(self.discount_days or 0)) if self.early_discount else None

    @api.expose
    def preview(self, date_ref: str, total: float) -> list[dict[str, Any]]:
        """Due dates and amounts for an example total (shown on the payment terms form)."""
        self.ensure_one()
        day = dt.date.fromisoformat(date_ref)
        currency = (self.env.company or self.env.ref("core.main_company")).elevated().currency_id
        return [
            {"date": r["date"].isoformat(), "amount": r["amount"]}
            for r in self.compute_installments(day, total, total, currency, currency)
        ]


class PaymentTermLine(Model):
    _name = "account.payment_term_line"
    _description = "Payment terms installment"
    _order = "payment_id, sequence, id"

    payment_id = fields.Many2one("account.payment_term", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    value = fields.Selection(
        [("percent", "Percent"), ("fixed", "Fixed amount"), ("balance", "Balance")], default="balance", required=True
    )
    value_amount = fields.Float(digits=(16, 4))
    nb_days = fields.Integer("Days", default=0)
    delay_type = fields.Selection(
        [
            ("days_after", "Days after invoice date"),
            ("days_after_end_of_month", "Days after end of month"),
            ("days_end_of_month_on_the", "On a day of the following month"),
        ],
        default="days_after",
        required=True,
    )
    days_next_month = fields.Integer("Day of the month", default=10)

    def due_date(self, date_ref: dt.date) -> dt.date:
        self.ensure_one()
        days = int(self.nb_days or 0)
        if self.delay_type == "days_after_end_of_month":
            return _end_of_month(date_ref) + dt.timedelta(days=days)
        if self.delay_type == "days_end_of_month_on_the":
            nxt = _end_of_month(date_ref) + dt.timedelta(days=1)
            day = min(max(int(self.days_next_month or 1), 1), calendar.monthrange(nxt.year, nxt.month)[1])
            return nxt.replace(day=day)
        return date_ref + dt.timedelta(days=days)
