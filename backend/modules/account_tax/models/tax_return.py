"""Tax returns: one record per company and tax period, with the report snapshot and the closing entry."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account_tax.closing import (
    closing_lines,
    ledger_check,
    period_of,
    report_totals,
    tax_report,
)
from tessera.exceptions import AccessError, UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


def _as_date(value: Any) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError as exc:
        raise UserError(_("Dates use YYYY-MM-DD")) from exc


class CompanyTax(Model):
    _extend = "core.company"

    account_tax_periodicity = fields.Selection(
        [("monthly", "Monthly"), ("quarterly", "Quarterly")], "Tax return periodicity", default="quarterly"
    )
    account_tax_closing_journal_id = fields.Many2one(
        "account.journal", "Tax closing journal", ondelete="set_null", help="Empty = the first miscellaneous journal"
    )


class TaxReturn(Model):
    _name = "account.tax_return"
    _description = "Tax return"
    _order = "date_to desc, id desc"
    _audit = True

    name = fields.Char(required=True, default="/")
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        index=True,
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    company_currency_id = fields.Many2one("core.currency", related="company_id.currency_id", string="Currency")
    date_from = fields.Date("Start", required=True)
    date_to = fields.Date("End", required=True)
    state = fields.Selection(
        [("draft", "Open"), ("closed", "Closed")], default="draft", required=True, readonly=True, copy=False
    )
    closing_move_id = fields.Many2one("account.move", "Closing entry", readonly=True, ondelete="restrict", copy=False)
    report_id = fields.Many2one("account.report", "Report", readonly=True, ondelete="set_null")
    report_snapshot = fields.Json("Report at closing", readonly=True, copy=False)
    amount_due = fields.Monetary("Tax due", currency_field="company_currency_id", readonly=True, copy=False)
    amount_deductible = fields.Monetary(
        "Deductible tax", currency_field="company_currency_id", readonly=True, copy=False
    )
    amount_net = fields.Monetary("Net tax payable", currency_field="company_currency_id", readonly=True, copy=False)
    findings = fields.Json("Pre-filing findings", readonly=True, copy=False)
    findings_summary = fields.Text("Pre-filing checks", readonly=True, copy=False)
    closed_by_id = fields.Many2one("core.user", "Closed by", readonly=True, ondelete="set_null", copy=False)
    closed_on = fields.Datetime(readonly=True, copy=False)

    @api.constrains("date_from", "date_to", "company_id")
    def _check_dates(self) -> None:
        for ret in self:
            if ret.date_to < ret.date_from:
                raise ValidationError(_("A tax period ends after it starts"), field="date_to")
            overlap = self.elevated().search_count(
                [
                    ("id", "!=", ret.ids[0]),
                    ("company_id", "=", ret.company_id.ids[0]),
                    ("date_from", "<=", ret.date_to),
                    ("date_to", ">=", ret.date_from),
                ]
            )
            if overlap:
                raise ValidationError(_("Tax returns of a company cannot overlap"), field="date_from")

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            company = (
                self.env["core.company"].browse([int(vals["company_id"])])
                if vals.get("company_id")
                else self.env.company
            )
            if not vals.get("date_from") or not vals.get("date_to"):
                start, end = self.next_period(company)
                vals.setdefault("date_from", start)
                vals.setdefault("date_to", end)
            if not vals.get("name") or vals["name"] == "/":
                vals["name"] = _(
                    "Tax return {start} - {end}",
                    start=_as_date(vals["date_from"]).isoformat(),
                    end=_as_date(vals["date_to"]).isoformat(),
                )
        return super().create(batch)

    def write(self, vals: dict[str, Any]) -> bool:
        if {"date_from", "date_to", "company_id"} & set(vals) and any(r.state == "closed" for r in self):
            raise UserError(_("The period of a closed tax return cannot change"))
        return super().write(vals)

    def unlink(self) -> bool:
        if any(r.state == "closed" for r in self):
            raise UserError(_("A closed tax return cannot be deleted"))
        return super().unlink()

    @api.model
    def next_period(self, company: Model | None = None) -> tuple[dt.date, dt.date]:
        """The period after the company's last return, else the last complete period before today."""
        company = company or self.env.company
        last = self.elevated().search([("company_id", "=", company.ids[0])], order="date_to desc", limit=1)
        if last:
            return period_of(company, last.date_to + dt.timedelta(days=1))
        start, _end = period_of(company, self.env.today())
        return period_of(company, start - dt.timedelta(days=1))

    # -- actions ---------------------------------------------------------------------------------------

    def _snapshot(self) -> dict[str, Any]:
        self.ensure_one()
        data = tax_report(self.env, self.company_id, self.date_from, self.date_to)
        totals = report_totals(data)
        report = self.env["account.report"].elevated().search([("code", "=", data["report"]["code"])], limit=1)
        return {
            "report_snapshot": data,
            "report_id": report.ids[0] if report else None,
            "amount_due": totals["TAX_DUE"],
            "amount_deductible": totals["TAX_DEDUCTIBLE"],
            "amount_net": totals["NET"],
        }

    @api.expose
    def action_refresh(self) -> bool:
        """Recompute the report figures and run the pre-filing checks (open returns only)."""
        for ret in self:
            if ret.state == "closed":
                raise UserError(_("A closed tax return keeps the figures of its closing"))
            ret.write(ret._snapshot())
            ret.action_check()
        return True

    @api.expose
    def action_check(self) -> list[dict[str, Any]]:
        """Run the pre-filing checks; returns the findings (also stored on the return)."""
        from modules.account_tax.prefiling import prefiling_checks, summarise

        findings: list[dict[str, Any]] = []
        for ret in self:
            findings = prefiling_checks(ret.company_id, ret.date_from, ret.date_to)
            ret.elevated().write({"findings": findings, "findings_summary": summarise(findings)})
        return findings

    @api.expose
    def action_close(self) -> bool:
        self.ensure_one()
        self.action_close_period(self.date_from.isoformat(), self.date_to.isoformat(), self.company_id.ids[0])
        return True

    @api.model
    @api.expose
    def action_close_period(
        self, date_from: Any, date_to: Any, company_id: int | None = None, force: bool = False
    ) -> int:
        """Close the tax period: post the tax closing entry, lock taxes up to ``date_to``; returns the return id.

        Refused when the period is already closed (tax lock date on or after its start, or a closed return), and
        when the report does not tie to the tax accounts unless ``force`` (items without grids, manual entries
        on tax accounts)."""
        if not (self.env.is_system or self.env.user.has_group("account.group_account_manager")):
            raise AccessError(_("Only accounting advisers can close a tax period"))
        start, end = _as_date(date_from), _as_date(date_to)
        if end < start:
            raise UserError(_("A tax period ends after it starts"))
        company = self.env["core.company"].browse([int(company_id)]) if company_id else self.env.company
        company.check_access("read")
        sudo_company = company.elevated("tax closing")
        lock = sudo_company.tax_lock_date
        if lock and lock >= start:
            raise UserError(_("Taxes are already closed up to {date}", date=lock.isoformat()))
        returns = self.elevated()
        closed = returns.search_count(
            [
                ("company_id", "=", company.ids[0]),
                ("state", "=", "closed"),
                ("date_from", "<=", end),
                ("date_to", ">=", start),
            ]
        )
        if closed:
            raise UserError(_("This tax period is already closed"))
        ret = returns.search(
            [("company_id", "=", company.ids[0]), ("date_from", "=", start), ("date_to", "=", end)], limit=1
        )
        if not ret:
            ret = returns.create({"company_id": company.ids[0], "date_from": start, "date_to": end})
        check = ledger_check(self.env, company, start, end)
        if check["difference"] and not force:
            raise UserError(
                _(
                    "The tax report differs from the tax accounts by {difference}: run the pre-filing checks first",
                    difference=check["difference"],
                )
            )
        vals = ret.with_env(self.env)._snapshot()
        lines = closing_lines(self.env.elevated("tax closing"), company, start, end)
        if lines:
            journal = sudo_company.account_tax_closing_journal_id or self.env["account.journal"].elevated().search(
                [("type", "=", "general"), ("company_id", "=", company.ids[0])], order="sequence, id", limit=1
            )
            if not journal:
                raise UserError(_("The company has no miscellaneous journal for the tax closing entry"))
            move = (
                self.env["account.move"]
                .elevated("tax closing")
                .create(
                    {
                        "move_type": "entry",
                        "journal_id": journal.ids[0],
                        "company_id": company.ids[0],
                        "date": end,
                        "ref": _("Tax closing {start} - {end}", start=start.isoformat(), end=end.isoformat()),
                        "line_ids": [("create", line) for line in lines],
                    }
                )
            )
            move.action_post()
            vals["closing_move_id"] = move.ids[0]
        sudo_company.write({"tax_lock_date": end})
        vals.update({"state": "closed", "closed_by_id": self.env.uid, "closed_on": self.env.now()})
        ret.write(vals)
        return ret.ids[0]
