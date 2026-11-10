"""Registering payments against posted invoices (partial, batch, early-payment discount, other currency)."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model


def _payment_kind(move: Model) -> tuple[str, str]:
    partner_type = "customer" if move.move_type in ("out_invoice", "out_refund") else "supplier"
    inbound = move.move_type in ("out_invoice", "in_refund")
    return ("inbound" if inbound else "outbound"), partner_type


def _open_term_lines(moves: Model) -> Model:
    return moves.mapped("line_ids").filtered(
        lambda line: line.display_type == "payment_term" and not line.reconciled and line.amount_residual_currency != 0
    )


def _early_discount(move: Model, date: dt.date) -> tuple[float, list[dict[str, Any]]] | None:
    """(discount in invoice currency, write-off lines) when ``date`` is within the early-payment window."""
    term = move.payment_term_id.elevated()
    if not term or not term.early_discount or not move.invoice_date:
        return None
    limit = term.discount_date(move.invoice_date)
    if limit is None or date > limit:
        return None
    if abs(move.amount_residual - move.amount_total) > 1e-6:
        return None  # only on an invoice with nothing paid yet
    pct = float(term.discount_percentage or 0) / 100.0
    currency = move.currency_id
    company = move.company_id.elevated()
    inbound = move.move_type in ("out_invoice", "in_refund")
    discount_account = company.account_epd_discount_expense_id if inbound else company.account_epd_discount_income_id
    if not discount_account:
        raise UserError(_("Set the cash-discount accounts on the company"))
    sign = 1 if inbound else -1  # write-offs are debits when money comes in
    lines: list[dict[str, Any]] = []
    base_discount = float(currency.round(move.amount_untaxed * pct))
    if term.early_pay_discount_computation == "included":
        lines.append(
            {"account_id": discount_account.ids[0], "amount": sign * base_discount, "name": _("Cash discount")}
        )
        total = base_discount
        for tax_line in move.line_ids.filtered(lambda line: line.display_type == "tax"):
            share = float(currency.round(abs(tax_line.amount_currency) * pct))
            if share:
                lines.append(
                    {
                        "account_id": tax_line.account_id.ids[0],
                        "amount": sign * share,
                        "name": _("Cash discount: {tax}", tax=tax_line.name or ""),
                        "tax_line_id": tax_line.tax_line_id.ids[0] if tax_line.tax_line_id else None,
                        "tax_repartition_line_id": (
                            tax_line.tax_repartition_line_id.ids[0] if tax_line.tax_repartition_line_id else None
                        ),
                    }
                )
                total += share
        return total, lines
    discount = float(currency.round(move.amount_total * pct))
    return discount, [{"account_id": discount_account.ids[0], "amount": sign * discount, "name": _("Cash discount")}]


def register_payments(
    moves: Model,
    *,
    journal: Model | None = None,
    date: dt.date | None = None,
    amount: float | None = None,
    currency: Model | None = None,
    group: bool = True,
    writeoff_account: Model | None = None,
    writeoff_label: str | None = None,
    communication: str | None = None,
    early_discount: bool = True,
) -> Model:
    """Create, post and match payments for posted invoices.

    ``amount`` (in ``currency``) applies when one payment is created; less than the residual leaves the
    invoice partially paid unless ``writeoff_account`` closes the difference. With ``group`` one payment is
    created per partner, account and currency; otherwise one per invoice.
    """
    env = moves.env
    moves.check_access("read")
    if not moves:
        raise UserError(_("Select invoices to pay"))
    for move in moves:
        if move.state != "posted" or not move.is_invoice():
            raise UserError(_("Only posted invoices can be paid ({name})", name=move.display_name))
        if move.payment_state in ("paid", "in_payment", "reversed"):
            raise UserError(_("{name} is already paid", name=move.name))
    if len(set(moves.mapped("company_id").ids)) != 1:
        raise UserError(_("Invoices of different companies are paid separately"))
    company = moves[0].company_id.elevated()
    date = date or env.today()
    if journal is None:
        journal = env["account.journal"].search(
            [("type", "in", ["bank", "cash"]), ("company_id", "=", company.ids[0])], order="sequence, id", limit=1
        )
        if not journal:
            raise UserError(_("Create a bank or cash journal first"))
    groups: dict[tuple[Any, ...], Model] = defaultdict(lambda: env["account.move"])
    for move in moves:
        kind = _payment_kind(move)
        key = (
            (move.commercial_partner_id.ids[0], kind, move.currency_id.ids[0])
            if group
            else (move.ids[0], kind, move.currency_id.ids[0])
        )
        groups[key] |= move
    if amount is not None and len(groups) > 1:
        raise UserError(_("An amount can only be given when the invoices make a single payment"))
    payments = env["account.payment"]
    for (_partner, (payment_type, partner_type), currency_id), batch in groups.items():
        inv_currency = env["core.currency"].browse([currency_id])
        pay_currency = currency or journal.currency_id or inv_currency
        term_lines = _open_term_lines(batch)
        residual_cur = abs(sum(term_lines.mapped("amount_residual_currency")))
        writeoffs: list[dict[str, Any]] = []
        discount_total = 0.0
        if early_discount and amount is None:
            for move in batch:
                epd = _early_discount(move, date)
                if epd:
                    discount_total += epd[0]
                    writeoffs.extend(epd[1])
        due_in_invoice_currency = float(inv_currency.round(residual_cur - discount_total))
        if pay_currency.ids == inv_currency.ids:
            pay_amount = due_in_invoice_currency if amount is None else float(amount)
            counterpart: dict[str, Any] = {}
        else:
            pay_amount = (
                float(inv_currency.convert(due_in_invoice_currency, pay_currency, company, date))
                if amount is None
                else float(amount)
            )
            covered = (
                due_in_invoice_currency
                if amount is None
                else float(pay_currency.convert(float(amount), inv_currency, company, date))
            )
            counterpart = {"counterpart_currency_id": inv_currency.ids[0], "counterpart_amount_currency": covered}
            if writeoffs:
                # write-offs are expressed in the payment currency
                writeoffs = [
                    {**w, "amount": float(inv_currency.convert(w["amount"], pay_currency, company, date))}
                    for w in writeoffs
                ]
                counterpart["counterpart_amount_currency"] = covered + discount_total
        if amount is not None and writeoff_account is not None:
            difference = float(
                pay_currency.round(abs(sum(term_lines.mapped("amount_residual_currency"))) - float(amount))
            )
            if pay_currency.ids != inv_currency.ids:
                difference = float(
                    pay_currency.round(
                        float(inv_currency.convert(residual_cur, pay_currency, company, date)) - float(amount)
                    )
                )
                counterpart["counterpart_amount_currency"] = residual_cur
            if difference:
                sign = 1 if payment_type == "inbound" else -1
                writeoffs.append(
                    {
                        "account_id": writeoff_account.ids[0],
                        "amount": sign * difference,
                        "name": writeoff_label or _("Write-off"),
                    }
                )
        if pay_amount <= 0:
            raise UserError(_("Nothing to pay"))
        ref = communication or ", ".join(m.payment_reference or m.name for m in batch)
        payment = env["account.payment"].create(
            {
                "payment_type": payment_type,
                "partner_type": partner_type,
                "partner_id": batch[0].commercial_partner_id.ids[0],
                "amount": pay_amount,
                "currency_id": pay_currency.ids[0],
                "date": date,
                "journal_id": journal.ids[0],
                "ref": ref,
                "destination_account_id": term_lines[0].account_id.ids[0] if term_lines else None,
                "writeoff_lines": writeoffs or None,
                **counterpart,
            }
        )
        payment.action_post()
        counter_lines = payment.move_id.line_ids.filtered(
            lambda line, d=payment.destination_account_id: line.account_id.ids == d.ids
        )
        by_account: dict[int, Model] = defaultdict(lambda: env["account.move_line"])
        for line in term_lines | counter_lines:
            by_account[line.account_id.ids[0]] |= line
        for lines in by_account.values():
            lines.reconcile()
        payment._compute_reconciliation()
        payments |= payment
    return payments
