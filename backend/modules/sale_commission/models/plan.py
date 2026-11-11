"""Commission plans: who is paid (salespeople and sales teams over date ranges), on what (rules by product,
product category or everything), how much (rate and fixed amount per unit) and the accelerators reached by the
achievement of a target (``sale.commission_target`` bands). The computation is in ``modules.sale_commission.engine``.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

PERIODS = [("monthly", "Monthly"), ("quarterly", "Quarterly")]
BASES = [("invoiced", "Invoiced (posted customer invoices)"), ("paid", "Paid (invoices fully paid)")]
APPLIES = [("all", "All products"), ("category", "Product category"), ("product", "Product")]


def period_bounds(period: str, day: dt.date) -> tuple[dt.date, dt.date]:
    """First and last day of the month or quarter containing ``day``."""
    first_month = day.month if period == "monthly" else 3 * ((day.month - 1) // 3) + 1
    start = dt.date(day.year, first_month, 1)
    months = 1 if period == "monthly" else 3
    year, month = divmod(first_month - 1 + months, 12)
    end = dt.date(day.year + year, month + 1, 1) - dt.timedelta(days=1)
    return start, end


def period_label(period: str, start: dt.date) -> str:
    return f"{start.year}-{start.month:02d}" if period == "monthly" else f"{start.year}-Q{(start.month - 1) // 3 + 1}"


class CommissionPlan(Model):
    _name = "sale.commission_plan"
    _description = "Commission plan"
    _order = "sequence, name, id"
    _inherit = ["chatter.thread"]
    _track_create = False
    _sql_constraints = [("target_positive", "CHECK (target_amount >= 0)", "A target cannot be negative")]

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    period = fields.Selection(PERIODS, default="monthly", required=True, tracking=True)
    basis = fields.Selection(
        BASES,
        default="invoiced",
        required=True,
        tracking=True,
        help="Invoiced: invoices and credit notes count in the period they are posted. Paid: invoices count in the "
        "period they become fully paid",
    )
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).ids[0],
    )
    currency_id = fields.Many2one("core.currency", related="company_id.currency_id", string="Currency")
    target_amount = fields.Monetary(
        "Target per period",
        currency_field="currency_id",
        default=0.0,
        help="Untaxed amount each salesperson should reach per period (0: no accelerators)",
    )
    active = fields.Boolean(default=True)
    user_ids = fields.One2many("sale.commission_plan_user", "plan_id", "Salespeople and teams", copy=True)
    rule_ids = fields.One2many("sale.commission_rule", "plan_id", "Rates", copy=True)
    target_ids = fields.One2many("sale.commission_target", "plan_id", "Accelerators", copy=True)
    expense_account_id = fields.Many2one(
        "account.account",
        "Commission expense account",
        ondelete="set_null",
        help="Account of the commission bills (default: the chart's sales commissions account)",
    )
    statement_ids = fields.One2many("sale.commission_statement", "plan_id", "Statements")
    statement_count = fields.Integer("Statements", compute="_compute_statement_count")
    note = fields.Html("Notes")

    def _compute_statement_count(self) -> None:
        for plan in self:
            plan.statement_count = len(plan.statement_ids)

    def period_of(self, day: dt.date) -> tuple[dt.date, dt.date]:
        self.ensure_one()
        return period_bounds(self.period, day)

    def _assignments(self, date_from: dt.date, date_to: dt.date) -> Model:
        """Assignments overlapping the period."""
        self.ensure_one()
        return self.user_ids.filtered(
            lambda a: (not a.date_from or a.date_from <= date_to) and (not a.date_to or a.date_to >= date_from)
        )

    def assigned_users(self, date_from: dt.date, date_to: dt.date) -> Model:
        """Salespeople the plan covers during the period (team members included)."""
        users: list[int] = []
        for assignment in self._assignments(date_from, date_to):
            for uid in assignment._user_ids():
                if uid not in users:
                    users.append(uid)
        return self.env["core.user"].browse(users)

    @api.expose
    def action_generate_statements(self, date: str | None = None) -> dict[str, Any]:
        """Create (or recompute the draft) statements of the period containing ``date`` for every salesperson of
        these plans (default: the last complete period)."""
        from modules.sale_commission.engine import generate_statements

        ids: list[int] = []
        for plan in self:
            if date:
                try:
                    day = dt.date.fromisoformat(str(date)[:10])
                except ValueError as exc:
                    raise UserError(_("Use an ISO 8601 date")) from exc
            else:
                start, _end = plan.period_of(self.env.today())
                day = start - dt.timedelta(days=1)
            ids += generate_statements(plan, day).ids
        return {"statement_ids": ids}


class CommissionPlanUser(Model):
    _name = "sale.commission_plan_user"
    _description = "Commission plan assignment"
    _order = "plan_id, date_from, id"

    plan_id = fields.Many2one("sale.commission_plan", "Plan", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="plan_id.company_id", store=True, index=True)
    user_id = fields.Many2one("core.user", "Salesperson", ondelete="cascade", index=True)
    team_id = fields.Many2one("crm.team", "Sales team", ondelete="cascade", help="Every member of the team")
    date_from = fields.Date("From", help="Empty: since always")
    date_to = fields.Date("Until", help="Empty: no end")
    target_amount = fields.Monetary(
        "Own target", currency_field="currency_id", help="Replaces the plan's target for these salespeople"
    )
    currency_id = fields.Many2one("core.currency", related="plan_id.company_id.currency_id")

    def _user_ids(self) -> list[int]:
        if self.user_id:
            return self.user_id.ids
        return list(self.team_id.elevated().member_user_ids.ids) if self.team_id else []

    def covers(self, user_id: int, day: dt.date) -> bool:
        return (
            user_id in self._user_ids()
            and (not self.date_from or self.date_from <= day)
            and (not self.date_to or self.date_to >= day)
        )

    @api.constrains("user_id", "team_id", "date_from", "date_to", "plan_id")
    def _check_assignment(self) -> None:
        for rec in self:
            if bool(rec.user_id) == bool(rec.team_id):
                raise ValidationError(_("Assign either a salesperson or a sales team"), field="user_id")
            if rec.date_from and rec.date_to and rec.date_to < rec.date_from:
                raise ValidationError(_("The assignment ends before it starts"), field="date_to")
            if not rec.user_id:
                continue
            # a salesperson is in one plan of a company at a time (commissions would be paid twice)
            others = self.elevated().search(
                [
                    ("user_id", "=", rec.user_id.ids[0]),
                    ("id", "!=", rec.ids[0]),
                    ("plan_id.company_id", "=", rec.plan_id.elevated().company_id.ids[0]),
                    ("plan_id.active", "=", True),
                ]
            )
            for other in others:
                if (not other.date_from or not rec.date_to or other.date_from <= rec.date_to) and (
                    not other.date_to or not rec.date_from or other.date_to >= rec.date_from
                ):
                    raise ValidationError(
                        _(
                            "{user} is already in the plan {plan} for these dates",
                            user=rec.user_id.elevated().name,
                            plan=other.plan_id.name,
                        ),
                        field="user_id",
                    )


class CommissionRule(Model):
    _name = "sale.commission_rule"
    _description = "Commission rate"
    _order = "plan_id, sequence, id"
    _sql_constraints = [
        ("rate_range", "CHECK (rate >= 0 AND rate <= 100)", "A commission rate is between 0 and 100 %"),
        ("fixed_positive", "CHECK (fixed_amount >= 0)", "A fixed commission cannot be negative"),
    ]

    plan_id = fields.Many2one("sale.commission_plan", "Plan", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="plan_id.company_id", store=True, index=True)
    currency_id = fields.Many2one("core.currency", related="plan_id.company_id.currency_id")
    sequence = fields.Integer(default=10)
    applies_to = fields.Selection(APPLIES, "Applies to", default="all", required=True)
    category_id = fields.Many2one("product.category", "Product category", ondelete="cascade")
    product_id = fields.Many2one("product.product", "Product", ondelete="cascade")
    rate = fields.Float("Rate %", digits=(6, 3), default=0.0)
    fixed_amount = fields.Monetary(
        "Fixed per unit", currency_field="currency_id", default=0.0, help="Added for each unit sold (credited back)"
    )

    @api.constrains("applies_to", "category_id", "product_id")
    def _check_target(self) -> None:
        for rule in self:
            if rule.applies_to == "category" and not rule.category_id:
                raise ValidationError(_("Choose the product category of the rate"), field="category_id")
            if rule.applies_to == "product" and not rule.product_id:
                raise ValidationError(_("Choose the product of the rate"), field="product_id")

    def describe(self) -> str:
        self.ensure_one()
        if self.applies_to == "product":
            return self.product_id.elevated().display_name
        if self.applies_to == "category":
            return self.category_id.elevated().complete_name or self.category_id.elevated().name
        return str(_("All products"))


class CommissionTarget(Model):
    _name = "sale.commission_target"
    _description = "Commission accelerator"
    _order = "plan_id, min_achievement, id"
    _sql_constraints = [
        ("min_positive", "CHECK (min_achievement >= 0)", "An achievement threshold cannot be negative"),
        ("multiplier_positive", "CHECK (multiplier >= 0)", "A multiplier cannot be negative"),
        (
            "unique_band",
            "UNIQUE (plan_id, min_achievement)",
            "Two accelerators of a plan start at the same achievement",
        ),
    ]

    plan_id = fields.Many2one("sale.commission_plan", "Plan", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="plan_id.company_id", store=True, index=True)
    min_achievement = fields.Float(
        "From achievement %", digits=(8, 2), default=0.0, help="Applies when the target is reached at least this much"
    )
    multiplier = fields.Float(digits=(6, 3), default=1.0, help="Applied to the whole period's commission")
