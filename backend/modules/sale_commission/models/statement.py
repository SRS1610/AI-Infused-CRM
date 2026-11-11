"""Commission statements: one per salesperson, plan and period; lines are the invoice and credit note lines that
earned (or cancelled) commission. Draft statements are recomputed at will; confirmed ones are frozen and paid
through a vendor bill to the salesperson; the statement is *paid* when that bill is."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from modules.sale_commission.models.plan import BASES, PERIODS, period_label
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields

STATES = [("draft", "Draft"), ("confirmed", "Confirmed"), ("paid", "Paid")]


class CommissionStatement(Model):
    _name = "sale.commission_statement"
    _description = "Commission statement"
    _order = "date_from desc, user_id, id"
    _inherit = ["chatter.thread"]
    _track_create = False
    _sql_constraints = [
        (
            "unique_period",
            "UNIQUE (plan_id, user_id, date_from)",
            "This salesperson already has a statement for the period",
        ),
    ]

    name = fields.Char(compute="_compute_name", store=True)
    user_id = fields.Many2one("core.user", "Salesperson", required=True, ondelete="restrict", index=True, readonly=True)
    plan_id = fields.Many2one(
        "sale.commission_plan", "Plan", required=True, ondelete="restrict", index=True, readonly=True
    )
    company_id = fields.Many2one("core.company", related="plan_id.company_id", store=True, index=True)
    currency_id = fields.Many2one("core.currency", related="plan_id.company_id.currency_id", string="Currency")
    period = fields.Selection(PERIODS, related="plan_id.period")
    basis = fields.Selection(BASES, related="plan_id.basis")
    date_from = fields.Date("From", required=True, readonly=True, index=True)
    date_to = fields.Date("Until", required=True, readonly=True)
    period_label = fields.Char("Period", compute="_compute_name", store=True)
    state = fields.Selection(STATES, default="draft", required=True, readonly=True, tracking=True, index=True)
    line_ids = fields.One2many("sale.commission_statement_line", "statement_id", "Lines", readonly=True)
    line_count = fields.Integer("Lines", compute="_compute_line_count")
    target_amount = fields.Monetary("Target", currency_field="currency_id", readonly=True)
    achievement = fields.Float("Achievement %", digits=(8, 2), readonly=True)
    multiplier = fields.Float(digits=(6, 3), readonly=True, default=1.0)
    base_amount = fields.Monetary("Commission base", currency_field="currency_id", readonly=True)
    raw_amount = fields.Monetary("Commission before accelerator", currency_field="currency_id", readonly=True)
    commission_amount = fields.Monetary("Commission", currency_field="currency_id", readonly=True)
    adjustment_amount = fields.Monetary(
        "Adjustments",
        currency_field="currency_id",
        default=0.0,
        tracking=True,
        help="Manual correction, positive or negative",
    )
    adjustment_note = fields.Char("Reason for the adjustment")
    total_amount = fields.Monetary(
        "Total", currency_field="currency_id", compute="_compute_total", store=True, tracking=True
    )
    computed_at = fields.Datetime("Computed on", readonly=True)
    bill_id = fields.Many2one("account.move", "Bill", readonly=True, ondelete="set_null", copy=False)

    @api.depends("plan_id", "user_id", "date_from")
    def _compute_name(self) -> None:
        for rec in self:
            plan = rec.plan_id.elevated()
            label = period_label(plan.period, rec.date_from) if plan and rec.date_from else ""
            rec.period_label = label
            rec.name = f"{plan.name or ''} · {rec.user_id.elevated().name or ''} · {label}"

    def _compute_line_count(self) -> None:
        for rec in self:
            rec.line_count = len(rec.line_ids)

    @api.depends("commission_amount", "adjustment_amount")
    def _compute_total(self) -> None:
        for rec in self:
            total = float(rec.commission_amount or 0.0) + float(rec.adjustment_amount or 0.0)
            rec.total_amount = float(rec.currency_id.round(total)) if rec.currency_id else total

    def write(self, vals: dict[str, Any]) -> bool:
        if {"adjustment_amount", "adjustment_note"} & set(vals) and not self.env.context.get("commission_internal"):
            for rec in self:
                if rec.state != "draft":
                    raise UserError(_("Only draft statements can be adjusted ({name})", name=rec.name))
        return super().write(vals)

    def unlink(self) -> bool:
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Only draft statements can be deleted ({name})", name=rec.name))
        return super().unlink()

    @api.expose
    def action_compute(self) -> bool:
        """Recompute draft statements from the ledger (same result when nothing changed)."""
        from modules.sale_commission.engine import compute_statement

        self.check_access("write")
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Only draft statements are recomputed ({name})", name=rec.name))
            compute_statement(rec)
        return True

    @api.expose
    def action_confirm(self) -> bool:
        self.check_access("write")
        for rec in self:
            if rec.state != "draft":
                raise UserError(_("Only draft statements can be confirmed ({name})", name=rec.name))
        self.with_context(commission_internal=True).write({"state": "confirmed"})
        return True

    @api.expose
    def action_draft(self) -> bool:
        self.check_access("write")
        for rec in self:
            if rec.state != "confirmed" or rec.bill_id:
                raise UserError(_("Only confirmed statements without a bill go back to draft ({name})", name=rec.name))
        self.with_context(commission_internal=True).write({"state": "draft"})
        return True

    @api.expose
    def action_create_bill(self) -> dict[str, Any]:
        """A vendor bill per salesperson (one line per statement) for these confirmed statements. Needs the
        billing rights."""
        from modules.account.chart import chart_ref

        self.check_access("write")
        moves = self.env["account.move"]
        moves.check_access_rights("create")
        groups: dict[tuple[int, int], list[Model]] = defaultdict(list)
        for rec in self:
            if rec.state != "confirmed":
                raise UserError(_("Confirm the statement before paying it ({name})", name=rec.name))
            if rec.bill_id:
                raise UserError(_("{name} already has a bill", name=rec.name))
            if rec.total_amount <= 0:
                raise UserError(
                    _(
                        "{name} has nothing to pay: carry a negative total to the next period as an adjustment",
                        name=rec.name,
                    )
                )
            partner = rec.user_id.elevated().partner_id
            if not partner:
                raise UserError(_("{user} has no contact to pay", user=rec.user_id.elevated().name))
            groups[(rec.company_id.ids[0], partner.ids[0])].append(rec)
        created = moves
        for (company_id, partner_id), statements in groups.items():
            company = self.env["core.company"].browse([company_id])
            journal = self.env["account.journal"].search(
                [("type", "=", "purchase"), ("company_id", "=", company_id)], order="sequence, id", limit=1
            )
            if not journal:
                raise UserError(_("Create a purchase journal first"))
            lines = []
            for rec in statements:
                account = rec.plan_id.expense_account_id or chart_ref(company.elevated(), "commissions")
                lines.append(
                    (
                        "create",
                        {
                            "name": str(_("Commissions: {name}", name=rec.name)),
                            "quantity": 1.0,
                            "price_unit": rec.total_amount,
                            "tax_ids": [("set", [])],
                            **({"account_id": account.ids[0]} if account else {}),
                        },
                    )
                )
            bill = moves.with_company(company).create(
                {
                    "move_type": "in_invoice",
                    "partner_id": partner_id,
                    "company_id": company_id,
                    "journal_id": journal.ids[0],
                    "invoice_date": self.env.today(),
                    "ref": ", ".join(r.period_label for r in statements),
                    "invoice_origin": ", ".join(r.name for r in statements),
                    "invoice_line_ids": lines,
                }
            )
            for rec in statements:
                rec.with_context(commission_internal=True).write({"bill_id": bill.ids[0]})
            created |= bill
        return {"bill_ids": created.ids}

    def _sync_with_bill(self) -> None:
        """Paid with the bill; back to confirmed when the bill is no longer paid or is cancelled."""
        for rec in self.elevated():
            bill = rec.bill_id
            if bill and bill.state == "cancel":
                rec.with_context(commission_internal=True).write({"state": "confirmed", "bill_id": None})
                continue
            paid = bool(bill) and bill.state == "posted" and bill.payment_state in ("paid", "in_payment")
            state = "paid" if paid else "confirmed"
            if rec.state not in (state, "draft"):
                rec.with_context(commission_internal=True).write({"state": state})

    def facts(self) -> dict[str, Any]:
        from modules.sale_commission.engine import statement_facts

        return statement_facts(self)


class MoveCommission(Model):
    _extend = "account.move"

    def write(self, vals: dict[str, Any]) -> bool:
        result = super().write(vals)
        if {"payment_state", "state"} & set(vals):
            bills = [m.ids[0] for m in self if m.move_type == "in_invoice"]
            if bills:
                statements = self.env["sale.commission_statement"].elevated().search([("bill_id", "in", bills)])
                statements._sync_with_bill()
        return result


class CommissionStatementLine(Model):
    _name = "sale.commission_statement_line"
    _description = "Commission statement line"
    _order = "statement_id, date, move_id, id"

    statement_id = fields.Many2one(
        "sale.commission_statement", "Statement", required=True, ondelete="cascade", index=True
    )
    company_id = fields.Many2one("core.company", related="statement_id.company_id", store=True, index=True)
    currency_id = fields.Many2one("core.currency", related="statement_id.company_id.currency_id")
    move_line_id = fields.Many2one("account.move_line", "Invoice line", ondelete="set_null", index=True)
    move_id = fields.Many2one("account.move", "Invoice", ondelete="set_null")
    move_type = fields.Selection([("out_invoice", "Invoice"), ("out_refund", "Credit note")], "Document")
    date = fields.Date("Counted on", help="Posting date (invoiced basis) or the date the invoice was fully paid")
    partner_id = fields.Many2one("contacts.partner", "Customer", ondelete="set_null")
    product_id = fields.Many2one("product.product", "Product", ondelete="set_null")
    category_id = fields.Many2one("product.category", "Category", ondelete="set_null")
    rule_id = fields.Many2one("sale.commission_rule", "Rate", ondelete="set_null")
    name = fields.Char("Description")
    quantity = fields.Float(digits=(16, 3))
    base_amount = fields.Monetary("Base", currency_field="currency_id", help="Untaxed, in company currency")
    rate = fields.Float("Rate %", digits=(6, 3))
    fixed_amount = fields.Monetary("Fixed per unit", currency_field="currency_id")
    raw_amount = fields.Monetary("Before accelerator", currency_field="currency_id")
    multiplier = fields.Float(digits=(6, 3))
    amount = fields.Monetary("Commission", currency_field="currency_id")
