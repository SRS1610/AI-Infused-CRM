"""Payments (ERD_PHASE_2 §3.5). Registering payments against invoices is in ``modules.account.payments``."""

from __future__ import annotations

from typing import Any

from modules.account.models.move import INTERNAL, _changed_server_fields, _refuse_server_fields, _trusted
from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class JournalPayments(Model):
    _extend = "account.journal"

    payment_through_outstanding = fields.Boolean(
        "Payments wait for bank matching",
        help="Payments are booked on the outstanding receipts / payments accounts until a bank statement line "
        "matches them; otherwise they are booked straight on the bank account",
    )


# set by posting and matching only (U-9 SEC-50/SEC-51): a ``move_id`` in a caller's values would let
# ``action_cancel`` cancel any journal entry, a ``state`` would make a posted payment without an entry
PAYMENT_SERVER_FIELDS = {
    "state": "draft",
    "name": "/",
    "move_id": None,
    "is_matched": False,
    "is_reconciled": False,
    "reconciled_invoice_ids": None,
}


class Payment(Model):
    _name = "account.payment"
    _description = "Payment"
    _order = "date desc, id desc"
    _inherit = ["chatter.thread"]
    _audit = True
    _global_search = ["name", "partner_id", "ref"]

    name = fields.Char("Number", readonly=True, copy=False, default="/", index=True)
    payment_type = fields.Selection(
        [("inbound", "Receive money"), ("outbound", "Send money")], required=True, default="inbound"
    )
    partner_type = fields.Selection(
        [("customer", "Customer"), ("supplier", "Vendor")], required=True, default="customer"
    )
    partner_id = fields.Many2one("contacts.partner", "Partner", ondelete="restrict", index=True, tracking=True)
    amount = fields.Monetary(required=True, tracking=True)
    currency_id = fields.Many2one(
        "core.currency",
        required=True,
        ondelete="restrict",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).elevated().currency_id.ids[0],
    )
    date = fields.Date(required=True, default=lambda self: self.env.today(), index=True)
    journal_id = fields.Many2one(
        "account.journal", required=True, ondelete="restrict", domain=[["type", "in", ["bank", "cash"]]]
    )
    company_id = fields.Many2one("core.company", related="journal_id.company_id", store=True, index=True)
    payment_method = fields.Selection(
        [("manual", "Manual"), ("electronic", "Electronic"), ("check", "Check"), ("sepa", "Bank transfer")],
        default="manual",
        required=True,
    )
    state = fields.Selection(
        [("draft", "Draft"), ("posted", "Posted"), ("cancel", "Cancelled")],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
        tracking=True,
    )
    move_id = fields.Many2one("account.move", "Journal entry", readonly=True, copy=False, ondelete="restrict")
    ref = fields.Char("Memo")
    destination_account_id = fields.Many2one("account.account", "Counterpart account", ondelete="restrict")
    counterpart_currency_id = fields.Many2one("core.currency", ondelete="restrict", copy=False)
    counterpart_amount_currency = fields.Monetary(currency_field="counterpart_currency_id", copy=False)
    writeoff_lines = fields.Json(
        copy=False, help="[{account_id, amount, name, tax_line_id?}] (signed like the payment)"
    )
    reconciled_invoice_ids = fields.Many2many(
        "account.move",
        "Invoices",
        relation="account_payment_invoice_rel",
        column1="payment_id",
        column2="move_id",
        readonly=True,
    )
    is_matched = fields.Boolean("Matched with a bank statement", readonly=True, copy=False)
    is_reconciled = fields.Boolean("Fully allocated", readonly=True, copy=False)

    @api.constrains("amount")
    def _check_amount(self) -> None:
        for pay in self:
            if (pay.amount or 0) < 0:
                raise ValidationError(_("A payment amount cannot be negative"), field="amount")

    def _liquidity_account(self) -> Model:
        journal = self.journal_id.elevated()
        company = journal.company_id
        if journal.payment_through_outstanding:
            account = (
                company.account_outstanding_receipts_id
                if self.payment_type == "inbound"
                else company.account_outstanding_payments_id
            )
            if account:
                return account
        if not journal.default_account_id:
            raise UserError(_("Journal {name} has no bank / cash account", name=journal.name))
        return journal.default_account_id

    def _counterpart_account(self) -> Model:
        if self.destination_account_id:
            return self.destination_account_id
        company = self.journal_id.elevated().company_id
        if self.partner_id:
            name = (
                "property_account_receivable_id" if self.partner_type == "customer" else "property_account_payable_id"
            )
            return self.partner_id._accounting(company, name)
        return (
            company.default_receivable_account_id
            if self.partner_type == "customer"
            else company.default_payable_account_id
        )

    @api.expose
    def action_post(self) -> bool:
        for pay in self:
            if pay.state != "draft":
                raise UserError(_("Only draft payments can be posted"))
            if not pay.amount:
                raise UserError(_("A payment needs an amount"))
            journal = pay.journal_id.elevated()
            company = journal.company_id
            ccur = company.currency_id
            sign = 1 if pay.payment_type == "inbound" else -1
            amount_cur = sign * float(pay.amount)
            if pay.currency_id.ids == ccur.ids:
                balance = float(ccur.round(amount_cur))
            else:
                balance = float(pay.currency_id.convert(amount_cur, ccur, company, pay.date))
            partner_id = pay.partner_id.commercial_partner_id.ids[0] if pay.partner_id else None
            if pay.partner_id and not partner_id:
                partner_id = pay.partner_id.ids[0]
            lines: list[tuple[str, dict[str, Any]]] = [
                (
                    "create",
                    {
                        "name": pay.ref or _("Payment"),
                        "account_id": pay._liquidity_account().ids[0],
                        "partner_id": partner_id,
                        "balance": balance,
                        "amount_currency": amount_cur,
                        "currency_id": pay.currency_id.ids[0],
                    },
                )
            ]
            writeoff_total = 0.0
            for wo in pay.writeoff_lines or []:
                wo_cur = float(wo["amount"])
                wo_bal = (
                    float(ccur.round(wo_cur))
                    if pay.currency_id.ids == ccur.ids
                    else float(pay.currency_id.convert(wo_cur, ccur, company, pay.date))
                )
                writeoff_total += wo_bal
                lines.append(
                    (
                        "create",
                        {
                            "name": wo.get("name") or _("Write-off"),
                            "account_id": int(wo["account_id"]),
                            "partner_id": partner_id,
                            "balance": wo_bal,
                            "amount_currency": wo_cur,
                            "currency_id": pay.currency_id.ids[0],
                            "tax_line_id": wo.get("tax_line_id"),
                            "tax_repartition_line_id": wo.get("tax_repartition_line_id"),
                        },
                    )
                )
            counter_balance = -float(ccur.round(balance + writeoff_total))
            counter_currency = pay.counterpart_currency_id or pay.currency_id
            if pay.counterpart_currency_id and pay.counterpart_amount_currency:
                counter_amount = -sign * abs(float(pay.counterpart_amount_currency))
            else:
                counter_amount = -(amount_cur + sum(float(wo["amount"]) for wo in pay.writeoff_lines or []))
            account = pay._counterpart_account()
            if not account:
                raise UserError(_("No receivable / payable account for this payment"))
            lines.append(
                (
                    "create",
                    {
                        "name": pay.ref or _("Payment"),
                        "account_id": account.ids[0],
                        "partner_id": partner_id,
                        "balance": counter_balance,
                        "amount_currency": counter_amount,
                        "currency_id": counter_currency.ids[0],
                        "date_maturity": pay.date,
                    },
                )
            )
            move = (
                self.env["account.move"]
                .elevated("payment entry")
                .create(
                    {
                        "move_type": "entry",
                        "journal_id": journal.ids[0],
                        "company_id": company.ids[0],
                        "currency_id": pay.currency_id.ids[0],
                        "date": pay.date,
                        "ref": pay.ref,
                        "partner_id": partner_id,
                        "line_ids": lines,
                    }
                )
            )
            move.line_ids.with_context(**{INTERNAL: True}).write({"payment_id": pay.ids[0]})
            move.action_post()
            pay.with_context(**{INTERNAL: True}).write(
                {"state": "posted", "move_id": move.ids[0], "name": move.name, "destination_account_id": account.ids[0]}
            )
            pay._compute_reconciliation()
        return True

    @api.expose
    def action_cancel(self) -> bool:
        for pay in self:
            if pay.move_id:
                if pay.ids[0] not in pay.move_id.elevated().line_ids.mapped("payment_id").ids:
                    raise UserError(_("This payment's journal entry was not booked by it"))
                lines = pay.move_id.line_ids
                if lines.filtered(lambda line: line.matched_debit_ids or line.matched_credit_ids):
                    lines.remove_move_reconcile()
                pay.move_id.elevated().button_cancel()
            pay.with_context(**{INTERNAL: True}).write({"state": "cancel"})
        return True

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        if not _trusted(self):
            for vals in batch:
                _refuse_server_fields(self, _changed_server_fields(self, vals, PAYMENT_SERVER_FIELDS))
        return super().create(batch)

    def write(self, vals: dict[str, Any]) -> bool:
        if not self.env.context.get(INTERNAL):
            if not self.env.su:
                _refuse_server_fields(self, set(PAYMENT_SERVER_FIELDS) & set(vals))
            for pay in self:
                if pay.state != "draft" and set(vals) - {"ref"}:
                    raise UserError(_("Posted payments cannot be modified; cancel them instead"))
        return super().write(vals)

    def unlink(self) -> bool:
        if self.filtered(lambda p: p.state == "posted" or p.move_id):
            raise UserError(_("Posted payments cannot be deleted; cancel them instead"))
        return super().unlink()

    def _compute_reconciliation(self) -> None:
        for pay in self:
            move = pay.move_id
            if not move:
                continue
            liquidity_account = pay._liquidity_account()
            liquidity = move.line_ids.filtered(lambda line, a=liquidity_account: line.account_id.ids == a.ids)
            counterpart = (
                move.line_ids.filtered(lambda line, d=pay.destination_account_id: line.account_id.ids == d.ids)
                if pay.destination_account_id
                else move.line_ids.browse([])
            )
            matched = all(line.reconciled for line in liquidity) if liquidity_account.reconcile else True
            invoices = counterpart._reconciled_counterparts().mapped("move_id").filtered(lambda m: m.is_invoice())
            vals = {
                "is_matched": matched,
                "is_reconciled": bool(counterpart) and all(line.reconciled for line in counterpart),
                "reconciled_invoice_ids": [("set", invoices.ids)],
            }
            pay.elevated().with_context(**{INTERNAL: True}).write(vals)
            if invoices:
                invoices._compute_payment_state()
