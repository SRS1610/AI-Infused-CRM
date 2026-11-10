"""Journal entries, invoices and journal items (ERD_PHASE_2 §3.3, invariants D-073).

Sign conventions: ``balance = debit - credit`` in company currency, ``amount_currency`` is the same amount in
the item's currency. On a customer invoice the income and tax lines are credits and the receivable line a
debit; vendor bills are the mirror image; credit notes swap both.

Invoices keep their *dynamic* lines (taxes, payment-term installments) in sync with the product lines: every
change to a draft invoice deletes and rebuilds them (``_sync_dynamic_lines``), so a stored invoice is always a
complete, balanced double entry.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
from collections import defaultdict
from typing import Any

from modules.account.models.tax import _round
from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

MOVE_TYPES = [
    ("entry", "Journal entry"),
    ("out_invoice", "Customer invoice"),
    ("out_refund", "Customer credit note"),
    ("in_invoice", "Vendor bill"),
    ("in_refund", "Vendor credit note"),
]
INVOICE_TYPES = ("out_invoice", "out_refund", "in_invoice", "in_refund")
SALE_TYPES = ("out_invoice", "out_refund")
PURCHASE_TYPES = ("in_invoice", "in_refund")
REFUND_TYPES = ("out_refund", "in_refund")
PAYMENT_STATES = [
    ("not_paid", "Not paid"),
    ("in_payment", "In payment"),
    ("paid", "Paid"),
    ("partial", "Partially paid"),
    ("reversed", "Reversed"),
]
DISPLAY_TYPES = [
    ("product", "Product"),
    ("tax", "Tax"),
    ("payment_term", "Payment term"),
    ("rounding", "Rounding"),
    ("epd", "Early-payment discount"),
    ("line_section", "Section"),
    ("line_note", "Note"),
]
DYNAMIC_TYPES = ("tax", "payment_term", "rounding")
# fields of a posted entry that may still change (D-073 invariant 3)
POSTED_WRITABLE = {
    "ref",
    "narration",
    "payment_state",
    "amount_residual",
    "amount_residual_signed",
    "invoice_user_id",
    "access_token",
    "to_check",
    "invoice_date_due",
    "auto_post_date",
    "message_follower_ids",
}
POSTED_LINE_WRITABLE = {
    "amount_residual",
    "amount_residual_currency",
    "reconciled",
    "full_reconcile_id",
    "name",
    "analytic_distribution",
    "date_maturity",
    "parent_state",
    "blocked",
}
INTERNAL = "_account_internal"  # context key set by the ledger's own writes


def _company_of(model: Model) -> Model:
    return model.env.company or model.env.ref("core.main_company")


class Move(Model):
    _name = "account.move"
    _description = "Journal entry"
    _order = "date desc, name desc, id desc"
    _inherit = ["chatter.thread"]
    _audit = True
    _global_search = ["name", "ref", "partner_id"]
    _indexes = [("journal_date", ["journal_id", "date"]), ("partner_state", ["commercial_partner_id", "state"])]

    name = fields.Char("Number", copy=False, readonly=True, index=True, default="/")
    ref = fields.Char("Reference", copy=False, index="trigram")
    date = fields.Date("Accounting date", required=True, index=True, default=lambda self: self.env.today())
    move_type = fields.Selection(MOVE_TYPES, "Type", required=True, default="entry", readonly=True, index=True)
    journal_id = fields.Many2one("account.journal", required=True, ondelete="restrict", index=True)
    company_id = fields.Many2one(
        "core.company", required=True, ondelete="restrict", default=lambda self: _company_of(self).ids[0]
    )
    company_currency_id = fields.Many2one("core.currency", related="company_id.currency_id", string="Company currency")
    currency_id = fields.Many2one(
        "core.currency",
        required=True,
        ondelete="restrict",
        default=lambda self: _company_of(self).elevated().currency_id.ids[0],
    )
    partner_id = fields.Many2one("contacts.partner", "Partner", ondelete="restrict", index=True, tracking=True)
    commercial_partner_id = fields.Many2one(
        "contacts.partner", "Commercial entity", compute="_compute_commercial_partner", store=True, index=True
    )
    state = fields.Selection(
        [("draft", "Draft"), ("posted", "Posted"), ("cancel", "Cancelled")],
        default="draft",
        required=True,
        readonly=True,
        copy=False,
        tracking=True,
        index=True,
    )
    line_ids = fields.One2many("account.move_line", "move_id", "Journal items", copy=True)
    invoice_line_ids = fields.One2many(
        "account.move_line",
        "move_id",
        "Invoice lines",
        domain=[["display_type", "in", ["product", "line_section", "line_note"]]],
        copy=False,  # copied through line_ids
    )
    invoice_date = fields.Date("Invoice date", copy=False, index=True)
    invoice_date_due = fields.Date("Due date", copy=False, index=True)
    payment_term_id = fields.Many2one("account.payment_term", "Payment terms", ondelete="restrict")
    fiscal_position_id = fields.Many2one("account.fiscal_position", "Fiscal position", ondelete="restrict")
    invoice_origin = fields.Char("Source document", copy=False)
    invoice_user_id = fields.Many2one(
        "core.user", "Salesperson", ondelete="set_null", default=lambda self: self.env.uid, copy=False
    )
    narration = fields.Html("Terms and conditions")
    payment_reference = fields.Char("Payment reference", copy=False)
    amount_untaxed = fields.Monetary("Untaxed amount", readonly=True, copy=False)
    amount_tax = fields.Monetary("Tax", readonly=True, copy=False)
    amount_total = fields.Monetary("Total", readonly=True, copy=False, tracking=True)
    amount_residual = fields.Monetary("Amount due", readonly=True, copy=False)
    amount_untaxed_signed = fields.Monetary(currency_field="company_currency_id", readonly=True, copy=False)
    amount_tax_signed = fields.Monetary(currency_field="company_currency_id", readonly=True, copy=False)
    amount_total_signed = fields.Monetary(
        "Total (company currency)", currency_field="company_currency_id", readonly=True, copy=False
    )
    amount_residual_signed = fields.Monetary(
        "Amount due (company currency)", currency_field="company_currency_id", readonly=True, copy=False
    )
    payment_state = fields.Selection(
        PAYMENT_STATES, "Payment status", default="not_paid", readonly=True, copy=False, tracking=True, index=True
    )
    reversed_entry_id = fields.Many2one("account.move", "Reversal of", readonly=True, copy=False, ondelete="set_null")
    reversal_move_ids = fields.One2many("account.move", "reversed_entry_id", "Reversals")
    auto_post_date = fields.Date("Post automatically on", copy=False)
    to_check = fields.Boolean("To check", copy=False)
    inalterable_hash = fields.Char(readonly=True, copy=False)
    secure_sequence_number = fields.Integer(readonly=True, copy=False)
    posted_before = fields.Boolean(readonly=True, copy=False, help="Has been posted at least once (keeps its number)")
    access_token = fields.Char(copy=False, groups=("account.group_account_invoice", "core.group_system"))
    is_fx_revaluation = fields.Boolean(readonly=True, copy=False)

    # -- computed --------------------------------------------------------------------------------------

    @api.depends("partner_id")
    def _compute_commercial_partner(self) -> None:
        for move in self:
            partner = move.partner_id
            move.commercial_partner_id = (partner.commercial_partner_id or partner) if partner else None

    @api.depends("name", "move_type", "state", "partner_id")
    def _compute_display_name(self) -> None:
        labels = dict(MOVE_TYPES)
        for move in self:
            if move.name and move.name != "/":
                move.display_name = move.name
            else:
                move.display_name = _("Draft {kind}", kind=labels.get(move.move_type or "entry", ""))

    def is_invoice(self) -> bool:
        return self.move_type in INVOICE_TYPES

    def is_sale(self) -> bool:
        return self.move_type in SALE_TYPES

    def _direction(self) -> int:
        """Sign of product lines' ``amount_currency``: credits on customer invoices and vendor refunds."""
        return -1 if self.move_type in ("out_invoice", "in_refund") else 1

    # -- create / write / unlink -----------------------------------------------------------------------

    @api.model
    def _default_journal(self, move_type: str, company: Model) -> Model:
        jtype = {"out_invoice": "sale", "out_refund": "sale", "in_invoice": "purchase", "in_refund": "purchase"}.get(
            move_type, "general"
        )
        journal = (
            self.env["account.journal"]
            .elevated()
            .search([("type", "=", jtype), ("company_id", "=", company.ids[0])], order="sequence, id", limit=1)
        )
        if not journal:
            raise UserError(_("No {kind} journal: install a chart of accounts first", kind=jtype))
        return self.env["account.journal"].browse(journal.ids)

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            move_type = vals.get("move_type") or self.env.context.get("default_move_type") or "entry"
            vals["move_type"] = move_type
            company = (
                self.env["core.company"].browse([int(vals["company_id"])])
                if vals.get("company_id")
                else _company_of(self)
            )
            if not vals.get("journal_id"):
                vals["journal_id"] = self._default_journal(move_type, company).ids[0]
            if not vals.get("company_id"):
                vals["company_id"] = (
                    self.env["account.journal"].elevated().browse([int(vals["journal_id"])]).company_id.ids[0]
                )
            if move_type in INVOICE_TYPES and vals.get("partner_id"):
                partner = self.env["contacts.partner"].browse([int(vals["partner_id"])])
                comp = self.env["core.company"].browse([int(vals["company_id"])])
                field = "property_payment_term_id" if move_type in SALE_TYPES else "property_supplier_payment_term_id"
                if "payment_term_id" not in vals:
                    term = partner._accounting(comp, field)
                    vals["payment_term_id"] = term.ids[0] if term else None
                if "fiscal_position_id" not in vals:
                    fp = self.env["account.fiscal_position"]._get_for_partner(partner, comp)
                    vals["fiscal_position_id"] = fp.ids[0] if fp else None
            # lines given through invoice_line_ids default to product lines
            for key in ("invoice_line_ids", "line_ids"):
                for cmd in vals.get(key) or []:
                    if isinstance(cmd, list | tuple) and cmd and cmd[0] == "create":
                        cmd[-1].setdefault("display_type", "product")
        moves = super().create(batch)
        moves.filtered(lambda m: m.is_invoice())._sync_dynamic_lines()
        moves._compute_amounts()
        return moves

    def write(self, vals: dict[str, Any]) -> bool:
        internal = self.env.context.get(INTERNAL)
        if not internal:
            for move in self:
                if move.state == "posted" and set(vals) - POSTED_WRITABLE:
                    raise UserError(
                        _("Posted entry {name} cannot be modified; reset it to draft first", name=move.name),
                        fields=sorted(set(vals) - POSTED_WRITABLE),
                    )
                if move.state == "cancel" and set(vals) - {"ref", "narration", "to_check"}:
                    raise UserError(_("Cancelled entry {name} cannot be modified", name=move.name))
            if "state" in vals or "name" in vals:
                raise UserError(_("Use the Post, Reset to draft and Cancel actions to change an entry's state"))
        result = super().write(vals)
        if not internal and not self.env.context.get("_skip_sync"):
            tracked = {"invoice_line_ids", "line_ids", "partner_id", "currency_id", "date", "invoice_date"}
            tracked |= {"payment_term_id", "fiscal_position_id", "move_type", "company_id", "journal_id"}
            if tracked & set(vals):
                drafts = self.filtered(lambda m: m.state == "draft")
                drafts.filtered(lambda m: m.is_invoice())._sync_dynamic_lines()
                drafts._compute_amounts()
        return result

    def unlink(self) -> bool:
        for move in self:
            if move.posted_before or move.state != "draft":
                raise UserError(
                    _("Entry {name} has been posted and keeps its number: cancel or reverse it instead", name=move.name)
                )
        lines = self.mapped("line_ids")
        if lines:
            lines.with_context(**{INTERNAL: True}).unlink()
        return super().unlink()

    @api.constrains("journal_id", "company_id")
    def _check_journal_company(self) -> None:
        for move in self:
            if move.journal_id.elevated().company_id.ids != move.company_id.ids:
                raise ValidationError(_("The journal must belong to the entry's company"), field="journal_id")

    # -- invoice lines synchronisation -----------------------------------------------------------------

    def _rate_date(self) -> dt.date:
        return self.invoice_date or self.date or self.env.today()

    def _to_company(self, amount: float, currency: Model | None = None) -> float:
        currency = currency or self.currency_id
        company = self.company_id.elevated()
        if currency.ids == company.currency_id.ids:
            return float(company.currency_id.round(amount))
        return float(currency.convert(amount, company.currency_id, company, self._rate_date()))

    def _product_line_account(self, line: Model) -> Model:
        """Income / expense account of a product line: product, then journal default, mapped by fiscal position."""
        account = line.account_id
        if not account:
            product = line.product_id.elevated() if line.product_id else None
            company = self.company_id.elevated()
            if product is not None:
                name = "property_account_income_id" if self.is_sale() else "property_account_expense_id"
                account = product[name] if name in product._fields else account
                if account and account.company_id.ids != company.ids:
                    account = account.browse([])
            if not account:
                account = self.journal_id.elevated().default_account_id
            if not account:
                account = company.default_income_account_id if self.is_sale() else company.default_expense_account_id
            if self.fiscal_position_id:
                account = self.fiscal_position_id.elevated().map_account(account)
        return account

    def _sync_dynamic_lines(self) -> None:
        """Rebuild tax and payment-term lines of draft invoices from their product lines."""
        line_model = self.env["account.move_line"].with_context(**{INTERNAL: True})
        for move in self:
            if move.state != "draft" or not move.is_invoice():
                continue
            me = move.with_context(**{INTERNAL: True})
            currency = move.currency_id
            places = int(currency.decimal_places or 2)
            company = move.company_id.elevated()
            ccur = company.currency_id
            direction = move._direction()
            refund = move.move_type in REFUND_TYPES
            old = move.line_ids.filtered(lambda line: line.display_type in DYNAMIC_TYPES)
            if old:
                old.with_context(**{INTERNAL: True}).unlink()
            tax_totals: dict[tuple[Any, ...], dict[str, Any]] = {}
            product_lines = move.line_ids.filtered(lambda line: line.display_type == "product")
            for line in product_lines:
                account = move._product_line_account(line)
                price = float(line.price_unit or 0) * (1 - float(line.discount or 0) / 100.0)
                taxes = line.tax_ids
                if move.fiscal_position_id:
                    taxes = move.fiscal_position_id.elevated().map_tax(taxes)
                res = taxes.elevated().compute_all(
                    price, currency, float(line.quantity or 0), line.product_id, move.partner_id, is_refund=refund
                )
                amount_currency = direction * res["total_excluded"]
                vals: dict[str, Any] = {
                    "price_subtotal": res["total_excluded"],
                    "price_total": res["total_included"],
                    "amount_currency": amount_currency,
                    "balance": move._to_company(amount_currency),
                    "currency_id": currency.ids[0],
                    "tax_tag_ids": [("set", res["base_tags"])],
                    "partner_id": move.commercial_partner_id.ids[0] if move.commercial_partner_id else None,
                }
                if account.ids != line.account_id.ids:
                    vals["account_id"] = account.ids[0] if account else None
                if taxes.ids != line.tax_ids.ids:
                    vals["tax_ids"] = [("set", taxes.ids)]
                line.with_context(**{INTERNAL: True}).write(vals)
                for tax in res["taxes"]:
                    acc_id = tax["account_id"] or (account.ids[0] if account else None)
                    key = (tax["tax_repartition_line_id"], acc_id, tuple(sorted(tax["tag_ids"])))
                    slot = tax_totals.setdefault(
                        key,
                        {"tax_id": tax["id"], "name": tax["name"], "amount": 0.0, "base": 0.0, "tags": tax["tag_ids"]},
                    )
                    slot["amount"] = _round(slot["amount"] + tax["amount"], places)
                    slot["base"] = _round(slot["base"] + res["total_excluded"], places)
            new_lines: list[dict[str, Any]] = []
            partner_id = move.commercial_partner_id.ids[0] if move.commercial_partner_id else None
            for (rep_id, acc_id, _tags), slot in tax_totals.items():
                if not slot["amount"]:
                    continue
                amount_currency = direction * slot["amount"]
                new_lines.append(
                    {
                        "move_id": move.ids[0],
                        "display_type": "tax",
                        "name": slot["name"],
                        "account_id": acc_id,
                        "tax_line_id": slot["tax_id"],
                        "tax_repartition_line_id": rep_id,
                        "tax_tag_ids": [("set", list(slot["tags"]))],
                        "tax_base_amount": abs(move._to_company(slot["base"])),
                        "amount_currency": amount_currency,
                        "balance": move._to_company(amount_currency),
                        "currency_id": currency.ids[0],
                        "partner_id": partner_id,
                    }
                )
            if new_lines:
                line_model.create(new_lines)
            # payment-term installments balance the entry exactly, in both currencies
            others = move.line_ids.filtered(lambda line: line.display_type not in ("line_section", "line_note"))
            total = -_round(sum(others.mapped("amount_currency")), places)
            total_company = -float(ccur.round(sum(others.mapped("balance"))))
            if not move.partner_id:
                continue
            account_name = "property_account_receivable_id" if move.is_sale() else "property_account_payable_id"
            term_account = move.partner_id._accounting(company, account_name)
            if not term_account:
                raise UserError(_("The company has no default receivable / payable account; install a chart first"))
            date_ref = move.invoice_date or move.date or move.env.today()
            installments = move.payment_term_id.elevated().compute_installments(
                date_ref, total, total_company, currency, ccur
            )
            line_model.create(
                [
                    {
                        "move_id": move.ids[0],
                        "display_type": "payment_term",
                        "name": move.payment_reference or move.ref or "",
                        "account_id": term_account.ids[0],
                        "amount_currency": inst["amount"],
                        "balance": inst["amount_company"],
                        "currency_id": currency.ids[0],
                        "partner_id": partner_id,
                        "date_maturity": inst["date"],
                    }
                    for inst in installments
                ]
            )
            due = max((inst["date"] for inst in installments), default=None)
            me.write({"invoice_date_due": due})

    def _compute_amounts(self) -> None:
        for move in self:
            lines = move.line_ids
            ccur = move.company_id.elevated().currency_id
            sign = -1 if move.move_type in ("out_invoice", "in_refund") else 1
            vals: dict[str, Any]
            if move.is_invoice():
                product = lines.filtered(lambda line: line.display_type == "product")
                tax = lines.filtered(lambda line: line.display_type == "tax")
                term = lines.filtered(lambda line: line.display_type == "payment_term")
                untaxed = sign * sum(product.mapped("amount_currency"))
                taxed = sign * sum(tax.mapped("amount_currency"))
                posted = move.state == "posted"
                # *_signed: company currency, positive for money in (customer invoices, vendor credit notes)
                vals = {
                    "amount_untaxed": untaxed,
                    "amount_tax": taxed,
                    "amount_total": untaxed + taxed,
                    "amount_residual": abs(sum(term.mapped("amount_residual_currency"))) if posted else untaxed + taxed,
                    "amount_untaxed_signed": float(ccur.round(-sum(product.mapped("balance")))),
                    "amount_tax_signed": float(ccur.round(-sum(tax.mapped("balance")))),
                    "amount_total_signed": float(ccur.round(sum(term.mapped("balance")))),
                    "amount_residual_signed": float(
                        ccur.round(sum(term.mapped("amount_residual") if posted else term.mapped("balance")))
                    ),
                }
            else:
                total = sum(lines.filtered(lambda line: line.balance > 0).mapped("balance"))
                vals = {
                    "amount_untaxed": 0.0,
                    "amount_tax": 0.0,
                    "amount_total": total,
                    "amount_residual": 0.0,
                    "amount_total_signed": total,
                    "amount_untaxed_signed": 0.0,
                    "amount_tax_signed": 0.0,
                    "amount_residual_signed": 0.0,
                }
            move.with_context(**{INTERNAL: True}).write(vals)

    # -- posting -------------------------------------------------------------------------------------

    def _check_lock(self, date: dt.date, *, tax: bool = False, action: str = "post") -> None:
        lock = self.company_id.elevated()._lock_date_for(tax=tax, adviser=self._is_adviser())
        if lock and date <= lock:
            raise UserError(
                _(
                    "The period up to {lock} is locked: you cannot {action} {name} dated {date}",
                    lock=lock,
                    action=_("post") if action == "post" else (_("cancel") if action == "cancel" else _("modify")),
                    name=self.name if self.name and self.name != "/" else _("this entry"),
                    date=date,
                ),
                lock_date=str(lock),
            )

    def _is_adviser(self) -> bool:
        return bool(self.env.is_system or self.env.user.has_group("account.group_account_manager"))

    def _check_balanced(self) -> None:
        for move in self:
            ccur = move.company_id.elevated().currency_id
            lines = move.line_ids.filtered(lambda line: line.display_type not in ("line_section", "line_note"))
            if not lines:
                raise UserError(_("Entry {name} has no journal items", name=move.display_name))
            diff = float(ccur.round(sum(lines.mapped("balance"))))
            if abs(diff) > 1e-9:
                raise UserError(
                    _("Entry {name} is not balanced (difference {diff})", name=move.display_name, diff=diff),
                    difference=diff,
                )

    @api.expose
    def action_post(self) -> bool:
        for move in self.sorted(lambda m: (m.date, m.ids[0])):
            if move.state != "draft":
                raise UserError(_("Only draft entries can be posted ({name})", name=move.display_name))
            internal = move.with_context(**{INTERNAL: True})
            if move.is_invoice():
                if not move.partner_id:
                    raise UserError(_("Set a customer or vendor before posting"))
                if not move.invoice_line_ids.filtered(lambda line: line.display_type == "product"):
                    raise UserError(_("Add at least one invoice line"))
                updates: dict[str, Any] = {}
                if not move.invoice_date:
                    updates["invoice_date"] = move.env.today()
                    updates["date"] = updates["invoice_date"]
                elif move.is_sale() and move.date != move.invoice_date:
                    updates["date"] = move.invoice_date
                if updates:
                    internal.write(updates)
                    move._sync_dynamic_lines()
                    move._compute_amounts()
                if move.amount_total < 0:
                    raise UserError(_("The total of an invoice cannot be negative: create a credit note instead"))
            move._check_lock(move.date, tax=bool(move.line_ids.filtered(lambda line: line.tax_line_id or line.tax_ids)))
            for line in move.line_ids:
                if not line.account_id and line.display_type not in ("line_section", "line_note"):
                    raise UserError(_("Every journal item needs an account ({line})", line=line.name or ""))
                acc = line.account_id.elevated() if line.account_id else None
                if acc is not None:
                    if acc.deprecated:
                        raise UserError(_("Account {code} is deprecated", code=acc.code))
                    if acc.company_id.ids != move.company_id.ids:
                        raise UserError(_("Account {code} belongs to another company", code=acc.code))
                    if acc.account_type in ("asset_receivable", "liability_payable") and not line.partner_id:
                        raise UserError(_("Receivable and payable items need a partner ({code})", code=acc.code))
            move._check_balanced()
            vals: dict[str, Any] = {"state": "posted", "posted_before": True}
            if not move.name or move.name == "/":
                year, _start, _end = move.env["account.fiscal_year"].year_of(move.company_id, move.date)
                refund = move.move_type in REFUND_TYPES
                vals["name"] = move.journal_id.elevated()._sequence_for(year, refund).next_by_id()
            if move.is_sale() and not move.access_token:
                vals["access_token"] = secrets.token_urlsafe(24)
            internal.write(vals)
            move.line_ids.with_context(**{INTERNAL: True}).write({"parent_state": "posted"})
            move.line_ids._init_residuals()
            if move.journal_id.elevated().restrict_mode_hash_table:
                move._hash_entry()
            if move.partner_id and move.is_invoice():
                partner = move.commercial_partner_id.elevated()
                field = "customer_rank" if move.is_sale() else "supplier_rank"
                partner.write({field: (partner[field] or 0) + 1})
            move._compute_amounts()
            move._compute_payment_state()
        return True

    @api.expose
    def button_draft(self) -> bool:
        for move in self:
            if move.state == "draft":
                continue
            if move.inalterable_hash:
                raise UserError(_("{name} is in a hash-locked journal and cannot be reset to draft", name=move.name))
            move._check_lock(move.date, action="modify")
            if move.line_ids.filtered(lambda line: line.matched_debit_ids or line.matched_credit_ids):
                raise UserError(_("{name} has payments or matches: unreconcile it first", name=move.name))
            internal = move.with_context(**{INTERNAL: True})
            internal.write({"state": "draft"})
            move.line_ids.with_context(**{INTERNAL: True}).write({"parent_state": "draft"})
            move._compute_amounts()
            internal.write({"payment_state": "not_paid"})
        return True

    @api.expose
    def button_cancel(self) -> bool:
        """Cancel a draft or posted entry. Posted numbers are kept (no gaps); locked periods refuse (2-b)."""
        for move in self:
            if move.state == "cancel":
                continue
            if move.state == "posted":
                # a hash-locked entry can be cancelled (its content, which the hash covers, is unchanged)
                move._check_lock(move.date, action="cancel")
                if move.line_ids.filtered(lambda line: line.matched_debit_ids or line.matched_credit_ids):
                    raise UserError(_("{name} has payments or matches: unreconcile it first", name=move.name))
            move.with_context(**{INTERNAL: True}).write({"state": "cancel", "payment_state": "not_paid"})
            move.line_ids.with_context(**{INTERNAL: True}).write({"parent_state": "cancel"})
        return True

    # -- reversal / credit notes ---------------------------------------------------------------------

    @api.expose
    def action_reverse(self, reason: str | None = None, date: str | None = None, mode: str = "refund") -> list[int]:
        """Create credit notes / reversal entries.

        ``mode``: ``refund`` (draft credit note), ``cancel`` (post it and match it with the original),
        ``modify`` (``cancel`` + a draft copy of the original to correct).
        """
        when = dt.date.fromisoformat(date) if date else None
        created = self.env["account.move"]
        reverse_type = {
            "out_invoice": "out_refund",
            "out_refund": "out_invoice",
            "in_invoice": "in_refund",
            "in_refund": "in_invoice",
            "entry": "entry",
        }
        for move in self:
            if move.state != "posted":
                raise UserError(_("Only posted entries can be reversed"))
            label = _("Reversal of: {name}", name=move.name) + (f", {reason}" if reason else "")
            day = when or (move.env.today() if move.is_invoice() else move.date)
            if move.is_invoice():
                lines = [
                    (
                        "create",
                        {
                            "display_type": line.display_type,
                            "name": line.name,
                            "product_id": line.product_id.ids[0] if line.product_id else None,
                            "account_id": line.account_id.ids[0] if line.account_id else None,
                            "quantity": line.quantity,
                            "price_unit": line.price_unit,
                            "discount": line.discount,
                            "tax_ids": [("set", line.tax_ids.ids)],
                            "uom_id": line.uom_id.ids[0] if line.uom_id else None,
                            "sequence": line.sequence,
                        },
                    )
                    for line in move.invoice_line_ids
                ]
                vals = {
                    "move_type": reverse_type[move.move_type],
                    "partner_id": move.partner_id.ids[0],
                    "journal_id": move.journal_id.ids[0],
                    "company_id": move.company_id.ids[0],
                    "currency_id": move.currency_id.ids[0],
                    "invoice_date": day,
                    "date": day,
                    "ref": label,
                    "invoice_origin": move.invoice_origin,
                    "reversed_entry_id": move.ids[0],
                    "fiscal_position_id": move.fiscal_position_id.ids[0] if move.fiscal_position_id else None,
                    "payment_term_id": None,
                    "invoice_line_ids": lines,
                }
                reverse = self.env["account.move"].create(vals)
            else:
                reverse = self.env["account.move"].create(
                    {
                        "move_type": "entry",
                        "journal_id": move.journal_id.ids[0],
                        "company_id": move.company_id.ids[0],
                        "currency_id": move.currency_id.ids[0],
                        "date": day,
                        "ref": label,
                        "reversed_entry_id": move.ids[0],
                        "line_ids": [
                            (
                                "create",
                                {
                                    "name": line.name,
                                    "account_id": line.account_id.ids[0],
                                    "partner_id": line.partner_id.ids[0] if line.partner_id else None,
                                    "balance": -line.balance,
                                    "amount_currency": -line.amount_currency,
                                    "currency_id": line.currency_id.ids[0],
                                    "tax_line_id": line.tax_line_id.ids[0] if line.tax_line_id else None,
                                    "tax_repartition_line_id": (
                                        line.tax_repartition_line_id.ids[0] if line.tax_repartition_line_id else None
                                    ),
                                    "tax_tag_ids": [("set", line.tax_tag_ids.ids)],
                                    "date_maturity": line.date_maturity,
                                },
                            )
                            for line in move.line_ids
                            if line.display_type not in ("line_section", "line_note")
                        ],
                    }
                )
            created |= reverse
            if mode in ("cancel", "modify"):
                reverse.action_post()
                reverse._reconcile_with(move)
            if mode == "modify":
                created |= move.copy({"invoice_date": None, "date": move.env.today()})
        return created.ids

    def _reconcile_with(self, other: Model) -> None:
        """Match the open receivable / payable items of two entries account by account."""
        lines = (
            (self | other)
            .mapped("line_ids")
            .filtered(lambda line: line.account_id.reconcile and not line.reconciled and line.parent_state == "posted")
        )
        by_account: dict[tuple[int, int | None], Model] = defaultdict(lambda: self.env["account.move_line"])
        for line in lines:
            by_account[(line.account_id.ids[0], line.partner_id.ids[0] if line.partner_id else None)] |= line
        for group in by_account.values():
            if group.filtered(lambda line: line.balance > 0) and group.filtered(lambda line: line.balance < 0):
                group.reconcile()

    # -- payment state -------------------------------------------------------------------------------

    def _compute_payment_state(self) -> None:
        for move in self:
            if move.state != "posted" or not move.is_invoice():
                if move.payment_state != "not_paid" and move.state != "posted":
                    move.with_context(**{INTERNAL: True}).write({"payment_state": "not_paid"})
                continue
            term_lines = move.line_ids.filtered(lambda line: line.display_type == "payment_term")
            ccur = move.company_id.elevated().currency_id
            currency = move.currency_id
            residual = sum(term_lines.mapped("amount_residual_currency"))
            total = sum(term_lines.mapped("amount_currency"))
            if currency.round(residual) == 0 and term_lines and all(term_lines.mapped("reconciled")):
                counterparts = term_lines._reconciled_counterparts()
                if counterparts and all(c.move_id.reversed_entry_id.ids == move.ids for c in counterparts):
                    state = "reversed"
                elif counterparts and any(c._is_unsettled_payment() for c in counterparts):
                    state = "in_payment"
                else:
                    state = "paid"
            elif ccur.round(sum(term_lines.mapped("amount_residual"))) != ccur.round(
                sum(term_lines.mapped("balance"))
            ) or (currency.round(residual) != currency.round(total)):
                state = "partial"
            else:
                state = "not_paid"
            if move.payment_state != state:
                move.with_context(**{INTERNAL: True}).write({"payment_state": state})
        self._compute_amounts()

    # -- inalterability (D-073) ----------------------------------------------------------------------

    def _canonical(self) -> str:
        lines = [
            [
                line.account_id.code,
                line.partner_id.ids[0] if line.partner_id else None,
                f"{float(line.debit or 0):.4f}",
                f"{float(line.credit or 0):.4f}",
                f"{float(line.amount_currency or 0):.4f}",
                line.currency_id.name,
            ]
            for line in self.line_ids.sorted(lambda r: r.ids[0])
            if line.display_type not in ("line_section", "line_note")
        ]
        return json.dumps(
            [self.name, str(self.date), self.journal_id.code, self.company_id.ids[0], self.move_type, lines],
            separators=(",", ":"),
        )

    def _hash_entry(self) -> None:
        self.ensure_one()
        cr = self.env.cr
        cr.execute("SELECT pg_advisory_xact_lock(hashtext('account.hash'), :j)", {"j": self.journal_id.ids[0]})
        row = cr.fetchone(
            "SELECT inalterable_hash, secure_sequence_number FROM account_move "
            "WHERE journal_id = :j AND inalterable_hash IS NOT NULL ORDER BY secure_sequence_number DESC LIMIT 1",
            {"j": self.journal_id.ids[0]},
        )
        previous, number = (row[0], int(row[1])) if row else ("", 0)
        digest = hashlib.sha256((previous + self._canonical()).encode()).hexdigest()
        self.with_context(**{INTERNAL: True}).write({"inalterable_hash": digest, "secure_sequence_number": number + 1})

    # -- customer access -----------------------------------------------------------------------------

    def portal_url(self) -> str:
        self.ensure_one()
        token = self.elevated().access_token
        return f"/portal/invoices/{self.ids[0]}?token={token}"

    @api.expose
    def action_send(self, email_to: str | None = None) -> dict[str, Any]:
        """Email the invoice PDF with a link to view (and pay) it online."""
        from modules.account.sending import send_invoice

        return send_invoice(self, email_to)


class MoveLine(Model):
    _name = "account.move_line"
    _description = "Journal item"
    _order = "date desc, move_name desc, sequence, id"
    _rec_name = "name"
    _sql_constraints = [
        ("debit_positive", "CHECK (debit >= 0)", "Debits cannot be negative"),
        ("credit_positive", "CHECK (credit >= 0)", "Credits cannot be negative"),
        ("one_side", "CHECK (NOT (debit > 0 AND credit > 0))", "An item is either a debit or a credit"),
        ("balance_def", "CHECK (balance = debit - credit)", "Balance must equal debit minus credit"),
    ]
    _indexes = [
        ("account_partner_open", ["account_id", "partner_id", "reconciled"]),
        ("account_date", ["account_id", "date"]),
        ("company_date", ["company_id", "date"]),
    ]

    move_id = fields.Many2one("account.move", "Journal entry", required=True, ondelete="cascade", index=True)
    move_name = fields.Char(related="move_id.name", store=True, string="Entry number")
    move_type = fields.Selection(MOVE_TYPES, related="move_id.move_type", store=True)
    journal_id = fields.Many2one("account.journal", related="move_id.journal_id", store=True, index=True)
    company_id = fields.Many2one("core.company", related="move_id.company_id", store=True, index=True)
    company_currency_id = fields.Many2one("core.currency", related="move_id.company_id.currency_id")
    date = fields.Date(related="move_id.date", store=True, index=True)
    parent_state = fields.Selection(
        [("draft", "Draft"), ("posted", "Posted"), ("cancel", "Cancelled")], default="draft", index=True, readonly=True
    )
    sequence = fields.Integer(default=10)
    display_type = fields.Selection(DISPLAY_TYPES, default="product", required=True)
    account_id = fields.Many2one("account.account", "Account", ondelete="restrict", index=True)
    partner_id = fields.Many2one("contacts.partner", "Partner", ondelete="restrict", index=True)
    name = fields.Char("Label")
    product_id = fields.Many2one("product.product", "Product", ondelete="restrict")
    uom_id = fields.Many2one("product.uom", "Unit", ondelete="set_null")
    quantity = fields.Float(digits=(16, 4), default=1.0)
    price_unit = fields.Float("Unit price", digits=(16, 4))
    discount = fields.Float("Discount %", digits=(16, 2))
    tax_ids = fields.Many2many(
        "account.tax", "Taxes", relation="account_move_line_tax_rel", column1="line_id", column2="tax_id"
    )
    tax_line_id = fields.Many2one("account.tax", "Originator tax", ondelete="restrict", index=True)
    tax_repartition_line_id = fields.Many2one("account.tax_repartition_line", ondelete="restrict")
    tax_base_amount = fields.Monetary("Base amount", currency_field="company_currency_id")
    tax_tag_ids = fields.Many2many(
        "account.account_tag", "Tax grids", relation="account_move_line_tag_rel", column1="line_id", column2="tag_id"
    )
    debit = fields.Monetary(currency_field="company_currency_id", default=0.0)
    credit = fields.Monetary(currency_field="company_currency_id", default=0.0)
    balance = fields.Monetary(currency_field="company_currency_id", default=0.0)
    amount_currency = fields.Monetary("Amount in currency", currency_field="currency_id", default=0.0)
    currency_id = fields.Many2one("core.currency", ondelete="restrict")
    price_subtotal = fields.Monetary("Subtotal", currency_field="currency_id", readonly=True)
    price_total = fields.Monetary("Total", currency_field="currency_id", readonly=True)
    date_maturity = fields.Date("Due date", index=True)
    amount_residual = fields.Monetary("Residual", currency_field="company_currency_id", readonly=True)
    amount_residual_currency = fields.Monetary("Residual in currency", currency_field="currency_id", readonly=True)
    reconciled = fields.Boolean(readonly=True, index=True)
    full_reconcile_id = fields.Many2one("account.full_reconcile", "Matching", readonly=True, ondelete="set_null")
    matched_debit_ids = fields.One2many("account.partial_reconcile", "credit_move_id", "Matched debits")
    matched_credit_ids = fields.One2many("account.partial_reconcile", "debit_move_id", "Matched credits")
    blocked = fields.Boolean("No follow-up", help="Excluded from reminders (disputed)")
    payment_id = fields.Many2one("account.payment", "Originating payment", ondelete="set_null", index=True)

    @api.depends("move_id", "name", "account_id")
    def _compute_display_name(self) -> None:
        for line in self:
            parts = [line.move_id.name if line.move_id else "", line.name or ""]
            line.display_name = " - ".join(p for p in parts if p and p != "/") or line.account_id.display_name or ""

    # -- create / write ------------------------------------------------------------------------------

    @staticmethod
    def _normalise_amounts(vals: dict[str, Any]) -> None:
        if "balance" in vals and vals["balance"] is not None:
            bal = float(vals["balance"] or 0)
            vals["debit"], vals["credit"] = (bal, 0.0) if bal > 0 else (0.0, -bal)
        elif "debit" in vals or "credit" in vals:
            debit, credit = float(vals.get("debit") or 0), float(vals.get("credit") or 0)
            net = debit - credit
            vals["debit"], vals["credit"], vals["balance"] = (net, 0.0, net) if net > 0 else (0.0, -net, net)

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        moves = self.env["account.move"]
        if not self.env.context.get(INTERNAL):
            for move in moves.browse(list({int(v["move_id"]) for v in batch if v.get("move_id")})):
                if move.state != "draft":
                    raise UserError(_("Items can only be added to draft entries ({name})", name=move.name))
        for vals in batch:
            self._normalise_amounts(vals)
            move = moves.browse([int(vals["move_id"])]) if vals.get("move_id") else None
            if move is not None:
                if not vals.get("currency_id"):
                    vals["currency_id"] = move.currency_id.ids[0]
                ccur = move.company_id.elevated().currency_id
                if vals["currency_id"] == ccur.ids[0] and "amount_currency" not in vals:
                    vals["amount_currency"] = vals.get("balance", 0.0)
                elif vals["currency_id"] == ccur.ids[0] and "balance" not in vals and "amount_currency" in vals:
                    vals["balance"] = vals["amount_currency"]
                    self._normalise_amounts(vals)
                vals.setdefault("parent_state", move.state)
        lines = super().create(batch)
        lines._init_residuals()
        return lines

    def write(self, vals: dict[str, Any]) -> bool:
        vals = dict(vals)
        if not self.env.context.get(INTERNAL):
            for line in self:
                state = line.move_id.state
                if state != "draft" and set(vals) - POSTED_LINE_WRITABLE:
                    raise UserError(
                        _("Items of posted entry {name} cannot be modified", name=line.move_id.name),
                        fields=sorted(set(vals) - POSTED_LINE_WRITABLE),
                    )
        if {"balance", "debit", "credit"} & set(vals):
            self._normalise_amounts(vals)
        result = super().write(vals)
        if {"balance", "debit", "credit", "amount_currency"} & set(vals):
            self._init_residuals()
        return result

    def unlink(self) -> bool:
        if not self.env.context.get(INTERNAL):
            for line in self:
                if line.move_id.state != "draft":
                    raise UserError(_("Items of posted entry {name} cannot be deleted", name=line.move_id.name))
        return super().unlink()

    # -- residuals and matching ----------------------------------------------------------------------

    def _init_residuals(self) -> None:
        """Residual = amount minus everything matched (only on reconcilable accounts)."""
        for line in self:
            account = line.account_id.elevated() if line.account_id else None
            if account is None or not (
                account.reconcile or account.account_type in ("asset_cash", "liability_credit_card")
            ):
                vals = {"amount_residual": 0.0, "amount_residual_currency": 0.0, "reconciled": False}
            else:
                matched = sum(float(p.amount) for p in line.matched_credit_ids) - sum(
                    float(p.amount) for p in line.matched_debit_ids
                )
                matched_cur = sum(float(p.debit_amount_currency) for p in line.matched_credit_ids) - sum(
                    float(p.credit_amount_currency) for p in line.matched_debit_ids
                )
                ccur = line.company_id.elevated().currency_id
                cur = line.currency_id or ccur
                residual = float(ccur.round(float(line.balance or 0) - matched))
                residual_cur = float(cur.round(float(line.amount_currency or 0) - matched_cur))
                vals = {
                    "amount_residual": residual,
                    "amount_residual_currency": residual_cur,
                    "reconciled": bool(account.reconcile)
                    and line.parent_state == "posted"
                    and residual == 0
                    and residual_cur == 0
                    and bool(line.matched_credit_ids or line.matched_debit_ids),
                }
            if any(line[k] != v for k, v in vals.items()):
                super(MoveLine, line.with_context(**{INTERNAL: True})).write(vals)

    def _reconciled_counterparts(self) -> Model:
        out = self.env["account.move_line"]
        for line in self:
            out |= line.matched_debit_ids.mapped("debit_move_id") | line.matched_credit_ids.mapped("credit_move_id")
        return out - self

    def _is_unsettled_payment(self) -> bool:
        """A payment whose money has not been matched with a bank statement yet (invoice is *in payment*)."""
        payment = self.elevated().payment_id
        return bool(payment) and not payment.is_matched

    @api.expose
    def reconcile(self) -> dict[str, Any]:
        """Match these items (same account, posted, not reconciled). See ``modules.account.reconcile``."""
        from modules.account.reconcile import reconcile_lines

        return reconcile_lines(self)

    @api.expose
    def remove_move_reconcile(self) -> bool:
        from modules.account.reconcile import unreconcile_lines

        unreconcile_lines(self)
        return True
