"""Analytic plans, accounts, applicability rules, analytic items and distribution models (ERD_PHASE_2 §6).

Sign convention of analytic items: ``amount = -balance`` of the journal item share, in company currency, so
income is positive and costs are negative (a 1 000.00 sale shows +1 000.00, a 300.00 bill shows -300.00).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from modules.account.models.account import ACCOUNT_TYPES
from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

APPLICABILITY = [("optional", "Optional"), ("mandatory", "Mandatory")]


def _company_default(self: Model) -> int | None:
    return self.env.company.ids[0] if self.env.company else None


def parse_distribution(value: Any) -> dict[int, float]:
    """``{"12": 60, "14": 40}`` → ``{12: 60.0, 14: 40.0}``; raises ``ValidationError`` on malformed input."""
    if value in (None, "", {}):
        return {}
    if not isinstance(value, dict):
        raise ValidationError(
            _("An analytic distribution maps analytic account ids to percentages"), field="analytic_distribution"
        )
    out: dict[int, float] = {}
    for key, pct in value.items():
        try:
            account_id = int(key)
            percent = float(pct)
        except (TypeError, ValueError):
            raise ValidationError(
                _("Invalid analytic distribution entry {key}: {value}", key=key, value=pct),
                field="analytic_distribution",
            ) from None
        if percent <= 0 or percent > 100:
            raise ValidationError(
                _("Analytic percentages must be greater than 0 and at most 100 ({value})", value=percent),
                field="analytic_distribution",
            )
        out[account_id] = out.get(account_id, 0.0) + percent
    return out


def normalise_distribution(value: Any) -> dict[str, float] | None:
    """Canonical stored form: string keys, float percentages; ``None`` when empty."""
    parsed = parse_distribution(value)
    return {str(k): round(v, 4) for k, v in parsed.items()} or None


class AnalyticPlan(Model):
    _name = "account.analytic_plan"
    _description = "Analytic plan"
    _order = "sequence, name"

    name = fields.Char(required=True, translate=True)
    description = fields.Text()
    sequence = fields.Integer(default=10)
    color = fields.Integer("Colour", default=0)
    company_id = fields.Many2one("core.company", ondelete="cascade", help="Empty = shared by every company")
    default_applicability = fields.Selection(
        APPLICABILITY,
        required=True,
        default="optional",
        help="Whether journal items need an analytic account of this plan when no rule below applies",
    )
    applicability_ids = fields.One2many("account.analytic_applicability", "plan_id", "Applicability rules")
    account_ids = fields.One2many("account.analytic_account", "plan_id", "Analytic accounts")
    account_count = fields.Integer(compute="_compute_account_count")
    active = fields.Boolean(default=True)

    @api.depends("account_ids")
    def _compute_account_count(self) -> None:
        for plan in self:
            plan.account_count = len(plan.account_ids)

    def _applicability_for(self, account: Model | None, journal: Model | None, company: Model) -> str:
        """The applicability of this plan for a journal item: the most specific matching rule, else the default."""
        self.ensure_one()
        best: tuple[int, str] | None = None
        acc = account.elevated() if account else None
        for rule in self.elevated().applicability_ids:
            if rule.company_id and rule.company_id.ids != company.ids:
                continue
            score = 0
            if rule.account_type:
                if acc is None or acc.account_type != rule.account_type:
                    continue
                score += 1
            if rule.account_prefix:
                if acc is None or not (acc.code or "").startswith(rule.account_prefix):
                    continue
                score += 2
            if rule.journal_id:
                if journal is None or rule.journal_id.ids != journal.ids:
                    continue
                score += 1
            if best is None or score > best[0]:
                best = (score, rule.applicability)
        return best[1] if best else (self.default_applicability or "optional")


class AnalyticApplicability(Model):
    _name = "account.analytic_applicability"
    _description = "Analytic plan applicability"
    _order = "plan_id, id"

    plan_id = fields.Many2one("account.analytic_plan", "Plan", required=True, ondelete="cascade", index=True)
    account_type = fields.Selection(ACCOUNT_TYPES, "Account type")
    account_prefix = fields.Char("Account code prefix", help="Applies to accounts whose code starts with this")
    journal_id = fields.Many2one("account.journal", "Journal", ondelete="cascade")
    applicability = fields.Selection(APPLICABILITY, required=True, default="mandatory")
    company_id = fields.Many2one("core.company", ondelete="cascade")


class AnalyticAccount(Model):
    _name = "account.analytic_account"
    _description = "Analytic account"
    _order = "plan_id, code, name"
    _audit = True
    _global_search = ["code", "name"]
    _sql_constraints = [
        ("code_plan_company_uniq", "UNIQUE (code, plan_id, company_id)", "Analytic codes must be unique per plan")
    ]

    name = fields.Char(required=True, translate=True, index="trigram")
    code = fields.Char("Reference", index=True)
    plan_id = fields.Many2one("account.analytic_plan", "Plan", required=True, ondelete="restrict", index=True)
    partner_id = fields.Many2one("contacts.partner", "Customer", ondelete="set_null")
    company_id = fields.Many2one("core.company", ondelete="cascade", default=_company_default)
    currency_id = fields.Many2one("core.currency", compute="_compute_currency", string="Currency")
    line_ids = fields.One2many("account.analytic_line", "account_id", "Analytic items")
    debit = fields.Monetary(compute="_compute_balance", currency_field="currency_id", help="Sum of the costs")
    credit = fields.Monetary(compute="_compute_balance", currency_field="currency_id", help="Sum of the revenue")
    balance = fields.Monetary(
        compute="_compute_balance", currency_field="currency_id", help="Revenue minus costs (analytic items)"
    )
    active = fields.Boolean(default=True)

    @api.depends("code", "name")
    def _compute_display_name(self) -> None:
        for acc in self:
            acc.display_name = f"[{acc.code}] {acc.name}" if acc.code else acc.name

    @api.depends("company_id")
    def _compute_currency(self) -> None:
        for acc in self:
            company = acc.company_id or self.env.company
            acc.currency_id = company.elevated().currency_id.ids[0] if company else None

    @api.depends("line_ids", "line_ids.amount")
    def _compute_balance(self) -> None:
        sums: dict[int, tuple[float, float]] = {}
        ids = [i for i in self._ids if i]
        if ids:
            companies = self.env.companies.ids or self.env.company.ids
            rows = self.env.cr.execute(
                "SELECT account_id, COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0), "
                "COALESCE(SUM(amount) FILTER (WHERE amount < 0), 0) FROM account_analytic_line "
                "WHERE account_id = ANY(:ids) AND company_id = ANY(:companies) GROUP BY account_id",
                {"ids": ids, "companies": companies},
            ).all()
            sums = {int(r[0]): (float(r[1]), float(r[2])) for r in rows}
        for acc in self:
            pos, neg = sums.get(acc.ids[0], (0.0, 0.0))
            acc.credit = pos
            acc.debit = -neg
            acc.balance = pos + neg

    @api.constrains("company_id", "plan_id")
    def _check_plan_company(self) -> None:
        for acc in self:
            plan = acc.plan_id.elevated()
            if plan.company_id and acc.company_id and plan.company_id.ids != acc.company_id.ids:
                raise ValidationError(_("The analytic account must belong to its plan's company"), field="plan_id")

    def unlink(self) -> bool:
        if self._ids and self.env.cr.scalar(
            "SELECT 1 FROM account_analytic_line WHERE account_id = ANY(:ids) LIMIT 1", {"ids": list(self._ids)}
        ):
            raise ValidationError(_("An analytic account with analytic items cannot be deleted; archive it instead"))
        return super().unlink()


class AnalyticLine(Model):
    _name = "account.analytic_line"
    _description = "Analytic item"
    _order = "date desc, id desc"
    _indexes = [("account_date", ["account_id", "date"]), ("company_date", ["company_id", "date"])]

    name = fields.Char("Description", required=True)
    date = fields.Date(required=True, index=True, default=lambda self: self.env.today())
    amount = fields.Monetary(
        currency_field="currency_id", required=True, default=0.0, help="Income positive, costs negative"
    )
    unit_amount = fields.Float("Quantity", digits=(16, 4), default=0.0)
    account_id = fields.Many2one(
        "account.analytic_account", "Analytic account", required=True, ondelete="restrict", index=True
    )
    plan_id = fields.Many2one("account.analytic_plan", related="account_id.plan_id", store=True, string="Plan")
    general_account_id = fields.Many2one("account.account", "Financial account", ondelete="restrict", index=True)
    partner_id = fields.Many2one("contacts.partner", "Partner", ondelete="set_null", index=True)
    product_id = fields.Many2one("product.product", "Product", ondelete="set_null")
    move_line_id = fields.Many2one("account.move_line", "Journal item", ondelete="cascade", index=True)
    move_id = fields.Many2one("account.move", related="move_line_id.move_id", store=True, string="Journal entry")
    percentage = fields.Float(digits=(7, 4), help="Share of the journal item")
    ref = fields.Char("Reference")
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", default=_company_default)
    currency_id = fields.Many2one("core.currency", related="company_id.currency_id", string="Currency")

    @api.constrains("account_id", "company_id")
    def _check_company(self) -> None:
        for line in self:
            acc = line.account_id.elevated()
            if acc.company_id and acc.company_id.ids != line.company_id.ids:
                raise ValidationError(
                    _("Analytic account {name} belongs to another company", name=acc.display_name), field="account_id"
                )


class DistributionModel(Model):
    _name = "account.analytic_distribution_model"
    _description = "Analytic distribution model"
    _order = "sequence, id"

    sequence = fields.Integer(default=10)
    partner_id = fields.Many2one("contacts.partner", "Partner", ondelete="cascade")
    product_id = fields.Many2one("product.product", "Product", ondelete="cascade")
    product_categ_id = fields.Many2one(
        "product.category", "Product category", ondelete="cascade", help="Also applies to its sub-categories"
    )
    account_prefix = fields.Char("Account code prefix", help="e.g. 6 for every expense account")
    company_id = fields.Many2one("core.company", ondelete="cascade", default=_company_default)
    analytic_distribution = fields.Json(required=True, help='{"<analytic account id>": percent}')
    active = fields.Boolean(default=True)

    @api.constrains("analytic_distribution", "company_id")
    def _check_distribution(self) -> None:
        for model in self:
            _validate(self.env, model.analytic_distribution, model.company_id or None)

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if "analytic_distribution" in vals:
                vals["analytic_distribution"] = normalise_distribution(vals["analytic_distribution"])
        return super().create(batch)

    def write(self, vals: dict[str, Any]) -> bool:
        if "analytic_distribution" in vals:
            vals = {**vals, "analytic_distribution": normalise_distribution(vals["analytic_distribution"])}
        return super().write(vals)

    @api.model
    def distribution_for(self, line_vals: dict[str, Any]) -> dict[str, float]:
        """The distribution the models propose for a line (``{}`` when none matches).

        ``line_vals`` may hold ``partner_id``, ``product_id``, ``account_id`` (or ``account_code``) and
        ``company_id`` (ids). Every criterion a model sets must match; the most specific models win, and a
        less specific model only fills the plans the better ones leave empty.
        """
        env = self.env
        company_id = line_vals.get("company_id") or (env.company.ids[0] if env.company else None)
        product = (
            env["product.product"].elevated().browse([int(line_vals["product_id"])])
            if line_vals.get("product_id")
            else None
        )
        categ_ids: list[int] = []
        if product is not None and product.category_id:
            categ_ids = list(product.category_id._ancestor_ids())
        partner_ids: list[int] = []
        if line_vals.get("partner_id"):
            partner = env["contacts.partner"].elevated().browse([int(line_vals["partner_id"])])
            partner_ids = [partner.ids[0]]
            if partner.commercial_partner_id:
                partner_ids.append(partner.commercial_partner_id.ids[0])
        code = line_vals.get("account_code")
        if not code and line_vals.get("account_id"):
            code = env["account.account"].elevated().browse([int(line_vals["account_id"])]).code
        candidates: list[tuple[int, int, int, Model]] = []
        for model in self.elevated().search([]):
            if model.company_id and model.company_id.ids[0] != company_id:
                continue
            score = 0
            if model.partner_id:
                if model.partner_id.ids[0] not in partner_ids:
                    continue
                score += 1
            if model.product_id:
                if product is None or model.product_id.ids != product.ids:
                    continue
                score += 1
            if model.product_categ_id:
                if model.product_categ_id.ids[0] not in categ_ids:
                    continue
                score += 1
            if model.account_prefix:
                if not code or not code.startswith(model.account_prefix):
                    continue
                score += 1
            if model.company_id:
                score += 1
            candidates.append((-score, model.sequence or 0, model.ids[0], model))
        result: dict[str, float] = {}
        covered: set[int] = set()
        accounts = env["account.analytic_account"].elevated()
        for _score, _seq, _id, model in sorted(candidates, key=lambda c: c[:3]):
            dist = parse_distribution(model.analytic_distribution)
            by_plan: dict[int, dict[int, float]] = defaultdict(dict)
            for acc in accounts.browse(list(dist)):
                by_plan[acc.plan_id.ids[0]][acc.ids[0]] = dist[acc.ids[0]]
            for plan_id, shares in by_plan.items():
                if plan_id in covered:
                    continue
                covered.add(plan_id)
                for acc_id, pct in shares.items():
                    result[str(acc_id)] = pct
        return result


def _validate(env: Any, value: Any, company: Model | None) -> dict[int, float]:
    """Check a distribution: known accounts of the company, at most 100 % per plan."""
    dist = parse_distribution(value)
    if not dist:
        return dist
    accounts = env["account.analytic_account"].elevated().with_context(active_test=False).browse(list(dist)).exists()
    missing = set(dist) - set(accounts.ids)
    if missing:
        raise ValidationError(
            _("Unknown analytic account(s): {ids}", ids=", ".join(str(i) for i in sorted(missing))),
            field="analytic_distribution",
        )
    per_plan: dict[int, float] = defaultdict(float)
    names: dict[int, str] = {}
    for acc in accounts:
        if company is not None and acc.company_id and acc.company_id.ids != company.ids:
            raise ValidationError(
                _("Analytic account {name} belongs to another company", name=acc.display_name),
                field="analytic_distribution",
            )
        plan = acc.plan_id
        per_plan[plan.ids[0]] += dist[acc.ids[0]]
        names[plan.ids[0]] = plan.name
    for plan_id, total in per_plan.items():
        if total > 100.0001:
            raise ValidationError(
                _(
                    "The percentages of plan {plan} add up to {total} % (at most 100 %)",
                    plan=names[plan_id],
                    total=total,
                ),
                field="analytic_distribution",
            )
    return dist
