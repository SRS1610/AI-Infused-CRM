"""Budgets and budget-vs-actual (ERD_PHASE_2 §6).

A budget line plans an amount for a period on an analytic account (actual = its analytic items, optionally
restricted to some financial accounts or an account type) or, without analytic account, on financial
accounts or an account type (actual = posted journal items).

Signs: planned and actual amounts are positive for the line's nature. An *expense* line's actual is the
costs booked (debit minus credit; analytic: minus the analytic amount); a *revenue* line's actual is the
income booked (credit minus debit; analytic: the analytic amount). ``variance = actual - planned``: positive
is over budget for an expense line (unfavourable) and above target for a revenue line (favourable).

Actual, theoretical, achievement and variance are stored so that list, pivot and graph views can group them;
they are refreshed when a line changes, when analytic items or journal items they cover are posted or
removed, from the *Refresh* button and by the daily ``account_budget.refresh`` job (theoretical amounts move
with the date).
"""

from __future__ import annotations

import calendar
import datetime as dt
from typing import Any

from modules.account.models.account import ACCOUNT_TYPES
from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

STATES = [("draft", "Draft"), ("confirmed", "Confirmed"), ("done", "Done"), ("cancel", "Cancelled")]
LINE_TYPES = [("expense", "Expense"), ("revenue", "Revenue")]
ACTIVE_STATES = ("draft", "confirmed")
AMOUNT_FIELDS = ("practical_amount", "theoretical_amount", "achievement", "variance")
SOURCE_FIELDS = {"analytic_account_id", "account_ids", "account_type", "date_from", "date_to", "planned_amount"}
SOURCE_FIELDS |= {"line_type", "budget_id"}


# optional filters: financial accounts (:accounts) and account type (:atype)
ANALYTIC_ACTUAL_SQL = """
SELECT COALESCE(SUM(amount), 0) FROM account_analytic_line
 WHERE account_id = :analytic AND company_id = :company AND date BETWEEN :start AND :end
   AND (CAST(:accounts AS bigint[]) IS NULL OR general_account_id = ANY(CAST(:accounts AS bigint[])))
   AND (CAST(:atype AS varchar) IS NULL OR general_account_id IN
        (SELECT id FROM account_account WHERE account_type = CAST(:atype AS varchar)))
"""
LEDGER_ACTUAL_SQL = """
SELECT COALESCE(SUM(balance), 0) FROM account_move_line
 WHERE parent_state = 'posted' AND company_id = :company AND date BETWEEN :start AND :end
   AND (CAST(:accounts AS bigint[]) IS NULL OR account_id = ANY(CAST(:accounts AS bigint[])))
   AND (CAST(:atype AS varchar) IS NULL OR account_id IN
        (SELECT id FROM account_account WHERE account_type = CAST(:atype AS varchar)))
"""


def _company_default(self: Model) -> int | None:
    return self.env.company.ids[0] if self.env.company else None


def as_of_date(env: Any) -> dt.date:
    value = env.context.get("budget_date")
    if isinstance(value, str):
        return dt.date.fromisoformat(value)
    return value or env.today()


def elapsed_share(date_from: dt.date, date_to: dt.date, today: dt.date) -> float:
    """Share of the period elapsed at the end of ``today`` (0 before it starts, 1 once it is over)."""
    total = (date_to - date_from).days + 1
    if total <= 0 or today < date_from:
        return 0.0
    if today >= date_to:
        return 1.0
    return ((today - date_from).days + 1) / total


class Budget(Model):
    _name = "account.budget"
    _description = "Budget"
    _order = "date_from desc, name"
    _inherit = ["chatter.thread"]
    _audit = True

    name = fields.Char(required=True, tracking=True)
    date_from = fields.Date("Start", required=True)
    date_to = fields.Date("End", required=True)
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", default=_company_default)
    currency_id = fields.Many2one("core.currency", related="company_id.currency_id", string="Currency")
    user_id = fields.Many2one("core.user", "Responsible", ondelete="set_null", default=lambda self: self.env.uid)
    state = fields.Selection(STATES, default="draft", required=True, readonly=True, tracking=True, copy=False)
    line_ids = fields.One2many("account.budget_line", "budget_id", "Budget lines", copy=True)
    planned_total = fields.Monetary("Planned", compute="_compute_totals", currency_field="currency_id")
    practical_total = fields.Monetary("Actual", compute="_compute_totals", currency_field="currency_id")
    note = fields.Text("Notes")

    @api.depends("line_ids", "line_ids.planned_amount", "line_ids.practical_amount", "line_ids.line_type")
    def _compute_totals(self) -> None:
        for budget in self:
            planned = practical = 0.0
            for line in budget.line_ids:
                sign = 1 if line.line_type == "revenue" else -1
                planned += sign * float(line.planned_amount or 0)
                practical += sign * float(line.practical_amount or 0)
            budget.planned_total = planned
            budget.practical_total = practical

    @api.constrains("date_from", "date_to")
    def _check_dates(self) -> None:
        for budget in self:
            if budget.date_to < budget.date_from:
                raise ValidationError(_("A budget ends after it starts"), field="date_to")

    def _set_state(self, state: str, allowed: tuple[str, ...]) -> bool:
        for budget in self:
            if budget.state not in allowed:
                raise UserError(_("This budget cannot go from {old} to {new}", old=budget.state, new=state))
        self.write({"state": state})
        return True

    @api.expose
    def action_confirm(self) -> bool:
        for budget in self:
            if not budget.line_ids:
                raise UserError(_("Add at least one budget line before confirming"))
        self._set_state("confirmed", ("draft",))
        return self.action_refresh()

    @api.expose
    def action_done(self) -> bool:
        self.action_refresh()
        return self._set_state("done", ("confirmed",))

    @api.expose
    def action_cancel(self) -> bool:
        return self._set_state("cancel", ("draft", "confirmed"))

    @api.expose
    def action_draft(self) -> bool:
        return self._set_state("draft", ("cancel", "confirmed"))

    @api.expose
    def action_refresh(self) -> bool:
        self.mapped("line_ids")._refresh_amounts()
        return True

    @api.expose
    def action_split_monthly(self) -> list[int]:
        """Split every line spanning several months into one line per month."""
        created: list[int] = []
        for budget in self:
            if budget.state not in ACTIVE_STATES:
                raise UserError(_("Only draft or confirmed budgets can be changed"))
            for line in budget.line_ids:
                created += line.action_split_monthly()
        return created

    def unlink(self) -> bool:
        for budget in self:
            if budget.state not in ("draft", "cancel"):
                raise UserError(_("Only draft or cancelled budgets can be deleted"))
        return super().unlink()


class BudgetLine(Model):
    _name = "account.budget_line"
    _description = "Budget line"
    _order = "budget_id, sequence, date_from, id"
    _indexes = [("analytic_dates", ["analytic_account_id", "date_from", "date_to"])]

    budget_id = fields.Many2one("account.budget", "Budget", required=True, ondelete="cascade", index=True)
    name = fields.Char("Label")
    sequence = fields.Integer(default=10)
    line_type = fields.Selection(LINE_TYPES, "Type", required=True, default="expense")
    analytic_account_id = fields.Many2one(
        "account.analytic_account", "Analytic account", ondelete="restrict", index=True
    )
    account_ids = fields.Many2many(
        "account.account",
        "Financial accounts",
        relation="account_budget_line_account_rel",
        column1="line_id",
        column2="account_id",
    )
    account_type = fields.Selection(ACCOUNT_TYPES, "Account type")
    date_from = fields.Date("Start", required=True)
    date_to = fields.Date("End", required=True)
    planned_amount = fields.Monetary("Planned", currency_field="currency_id", required=True, default=0.0)
    practical_amount = fields.Monetary("Actual", currency_field="currency_id", readonly=True, copy=False)
    theoretical_amount = fields.Monetary(
        "Theoretical", currency_field="currency_id", readonly=True, copy=False, help="Planned x elapsed share"
    )
    achievement = fields.Float("Achievement %", digits=(16, 2), readonly=True, copy=False)
    variance = fields.Monetary(currency_field="currency_id", readonly=True, copy=False, help="Actual - planned")
    company_id = fields.Many2one("core.company", related="budget_id.company_id", store=True, index=True)
    currency_id = fields.Many2one("core.currency", related="budget_id.company_id.currency_id", string="Currency")
    budget_state = fields.Selection(STATES, related="budget_id.state", store=True, string="Budget state")

    @api.depends("name", "analytic_account_id", "budget_id")
    def _compute_display_name(self) -> None:
        for line in self:
            label = line.name or (line.analytic_account_id.display_name if line.analytic_account_id else "")
            line.display_name = f"{line.budget_id.name} - {label}" if label else line.budget_id.name

    # -- create / write ------------------------------------------------------------------------------

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        budgets = self.env["account.budget"]
        for vals in batch:
            if vals.get("budget_id") and not (vals.get("date_from") and vals.get("date_to")):
                budget = budgets.browse([int(vals["budget_id"])])
                vals["date_from"] = vals.get("date_from") or budget.date_from
                vals["date_to"] = vals.get("date_to") or budget.date_to
        lines = super().create(batch)
        lines._refresh_amounts()
        return lines

    def write(self, vals: dict[str, Any]) -> bool:
        if set(vals) & SOURCE_FIELDS:
            for line in self:
                if line.budget_id.state not in ACTIVE_STATES and not self.env.context.get("budget_refresh"):
                    raise UserError(_("Only draft or confirmed budgets can be changed"))
        result = super().write(vals)
        if set(vals) & SOURCE_FIELDS:
            self._refresh_amounts()
        return result

    @api.constrains("analytic_account_id", "account_ids", "account_type", "date_from")
    def _check_target(self) -> None:
        for line in self:
            if not (line.analytic_account_id or line.account_ids or line.account_type):
                raise ValidationError(
                    _("A budget line needs an analytic account, financial accounts or an account type"),
                    field="analytic_account_id",
                )

    @api.constrains("date_from", "date_to", "budget_id")
    def _check_dates(self) -> None:
        for line in self:
            if line.date_to < line.date_from:
                raise ValidationError(_("A budget line ends after it starts"), field="date_to")
            budget = line.budget_id
            if line.date_from < budget.date_from or line.date_to > budget.date_to:
                raise ValidationError(_("Budget lines must fall within the budget's period"), field="date_from")

    @api.constrains("account_ids", "analytic_account_id", "budget_id")
    def _check_company(self) -> None:
        for line in self:
            company = line.budget_id.company_id
            for acc in line.account_ids.elevated():
                if acc.company_id.ids != company.ids:
                    raise ValidationError(_("Account {code} belongs to another company", code=acc.code))
            analytic = line.analytic_account_id.elevated() if line.analytic_account_id else None
            if analytic is not None and analytic.company_id and analytic.company_id.ids != company.ids:
                raise ValidationError(_("The analytic account belongs to another company"), field="analytic_account_id")

    # -- actual amounts ------------------------------------------------------------------------------

    def _actual(self) -> float:
        """Actual amount of the line (positive for costs on expense lines, income on revenue lines)."""
        self.ensure_one()
        cr = self.env.cr
        params: dict[str, Any] = {
            "company": self.budget_id.company_id.ids[0],
            "start": self.date_from,
            "end": self.date_to,
            "accounts": self.account_ids.ids or None,
            "atype": self.account_type or None,
            "analytic": self.analytic_account_id.ids[0] if self.analytic_account_id else None,
        }
        if self.analytic_account_id:
            total = cr.scalar(ANALYTIC_ACTUAL_SQL, params)
            amount = float(total or 0)  # income positive
        else:
            total = cr.scalar(LEDGER_ACTUAL_SQL, params)
            amount = -float(total or 0)  # income positive
        return amount if self.line_type == "revenue" else -amount

    def _amounts(self, today: dt.date | None = None) -> dict[str, float]:
        self.ensure_one()
        today = today or as_of_date(self.env)
        currency = self.budget_id.company_id.elevated().currency_id
        planned = float(self.planned_amount or 0)
        practical = float(currency.round(self._actual()))
        share = elapsed_share(self.date_from, self.date_to, today)
        theoretical = float(currency.round(planned * share))
        return {
            "practical_amount": practical,
            "theoretical_amount": theoretical,
            "achievement": round(practical / planned * 100.0, 2) if planned else 0.0,
            "variance": float(currency.round(practical - planned)),
            "elapsed_share": share,
        }

    def _refresh_amounts(self) -> None:
        today = as_of_date(self.env)
        for line in self.elevated("budget actuals").with_context(budget_refresh=True):
            values = line._amounts(today)
            vals = {k: values[k] for k in AMOUNT_FIELDS if line[k] is None or abs(float(line[k]) - values[k]) > 1e-9}
            if vals:
                super(BudgetLine, line).write(vals)

    @api.model
    def _refresh_for(
        self, *, company_id: int, start: dt.date, end: dt.date, analytic_ids=None, account_ids=None
    ) -> None:
        """Refresh the active lines whose actuals may include items of these accounts in [start, end]."""
        domain: list[Any] = [
            ("company_id", "=", company_id),
            ("budget_state", "in", list(ACTIVE_STATES)),
            ("date_from", "<=", end),
            ("date_to", ">=", start),
        ]
        if analytic_ids is not None:
            domain.append(("analytic_account_id", "in", list(analytic_ids)))
        else:
            domain.append(("analytic_account_id", "=", None))
        lines = self.elevated("budget actuals").search(domain)
        if account_ids is not None and lines:
            accounts = self.env["account.account"].elevated().browse(list(account_ids))
            types = set(accounts.mapped("account_type"))
            lines = lines.filtered(
                lambda line: bool(set(line.account_ids.ids) & set(account_ids)) or line.account_type in types
            )
        if lines:
            lines._refresh_amounts()

    # -- monthly split -------------------------------------------------------------------------------

    @api.expose
    def action_split_monthly(self) -> list[int]:
        """Replace each line spanning several months by one line per month (planned amount pro rata of days)."""
        created: list[int] = []
        for line in self:
            if line.budget_id.state not in ACTIVE_STATES:
                raise UserError(_("Only draft or confirmed budgets can be changed"))
            months = month_slices(line.date_from, line.date_to)
            if len(months) < 2:
                continue
            currency = line.budget_id.company_id.elevated().currency_id
            total_days = (line.date_to - line.date_from).days + 1
            planned = float(line.planned_amount or 0)
            done_days = 0
            allocated = 0.0
            vals_list: list[dict[str, Any]] = []
            for start, end in months:
                done_days += (end - start).days + 1
                cumulative = float(currency.round(planned * done_days / total_days))
                vals_list.append(
                    {
                        "budget_id": line.budget_id.ids[0],
                        "name": f"{line.name or line.analytic_account_id.name or ''} {start:%Y-%m}".strip(),
                        "sequence": line.sequence,
                        "line_type": line.line_type,
                        "analytic_account_id": line.analytic_account_id.ids[0] if line.analytic_account_id else None,
                        "account_ids": [("set", line.account_ids.ids)],
                        "account_type": line.account_type,
                        "date_from": start,
                        "date_to": end,
                        "planned_amount": cumulative - allocated,
                    }
                )
                allocated = cumulative
            new = self.create(vals_list)
            created += new.ids
            line.unlink()
        return created


def month_slices(start: dt.date, end: dt.date) -> list[tuple[dt.date, dt.date]]:
    out: list[tuple[dt.date, dt.date]] = []
    cur = start
    while cur <= end:
        last = dt.date(cur.year, cur.month, calendar.monthrange(cur.year, cur.month)[1])
        out.append((cur, min(last, end)))
        cur = last + dt.timedelta(days=1)
    return out
