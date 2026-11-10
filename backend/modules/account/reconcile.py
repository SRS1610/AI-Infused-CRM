"""Matching journal items (ERD_PHASE_2 §3.5).

Debits and credits on one reconcilable account are matched oldest first (by due date, then date):

* two items in the **same foreign currency** are matched on their currency amounts; the company-currency
  amounts matched on each side are the proportional share of their own residuals, so an invoice and its
  payment booked at different rates both reach zero in currency while a company-currency difference remains;
  that difference is booked as a **realised exchange difference** (gain or loss) in the exchange journal and
  matched in, so both items end fully reconciled (2-c);
* otherwise items are matched on company-currency amounts, and the currency amounts follow proportionally.

When every item of the connected group has nothing left in either currency a full reconciliation is created.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from modules.account.models.move import INTERNAL
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model


def _key(line: Model) -> tuple[Any, ...]:
    return (line.date_maturity or line.date or dt.date.min, line.date or dt.date.min, line.ids[0])


def _check(lines: Model) -> None:
    if not lines:
        return
    accounts = set(lines.mapped("account_id").ids)
    if len(accounts) != 1:
        raise UserError(_("Only items of one account can be matched together"))
    account = lines[0].account_id.elevated()
    if not account.reconcile:
        raise UserError(_("Account {code} does not allow reconciliation", code=account.code))
    if len(set(lines.mapped("company_id").ids)) != 1:
        raise UserError(_("Items of different companies cannot be matched"))
    for line in lines:
        if line.parent_state != "posted":
            raise UserError(_("Only posted items can be matched ({name})", name=line.display_name))


def reconcile_lines(lines: Model) -> dict[str, Any]:
    lines.check_access("write")
    env = lines.env
    lines = lines.elevated("reconciliation").with_context(**{INTERNAL: True})
    open_lines = lines.filtered(lambda line: not line.reconciled)
    _check(open_lines)
    if not open_lines:
        return {"partials": 0, "full": False}
    company = open_lines[0].company_id.elevated()
    ccur = company.currency_id
    debits = sorted(
        [
            ln
            for ln in open_lines
            if ln.amount_residual > 0 or (ln.amount_residual == 0 and ln.amount_residual_currency > 0)
        ],
        key=_key,
    )
    credits = sorted(
        [
            ln
            for ln in open_lines
            if ln.amount_residual < 0 or (ln.amount_residual == 0 and ln.amount_residual_currency < 0)
        ],
        key=_key,
    )
    partial_model = env["account.partial_reconcile"].elevated().with_context(**{INTERNAL: True})
    created = partial_model.browse([])
    di = ci = 0
    while di < len(debits) and ci < len(credits):
        d, c = debits[di], credits[ci]
        dcur, ccur_line = d.currency_id or ccur, c.currency_id or ccur
        same_foreign = dcur.ids == ccur_line.ids and dcur.ids != ccur.ids
        if same_foreign:
            d_cur, c_cur = float(d.amount_residual_currency), -float(c.amount_residual_currency)
            if d_cur <= 0:
                di += 1
                continue
            if c_cur <= 0:
                ci += 1
                continue
            cur_amt = float(dcur.round(min(d_cur, c_cur)))
            d_amt = (
                float(d.amount_residual) if cur_amt == d_cur else float(ccur.round(d.amount_residual * cur_amt / d_cur))
            )
            c_amt = (
                -float(c.amount_residual)
                if cur_amt == c_cur
                else float(ccur.round(-c.amount_residual * cur_amt / c_cur))
            )
            amount = max(min(d_amt, c_amt), 0.0)
            vals = {"amount": amount, "debit_amount_currency": cur_amt, "credit_amount_currency": cur_amt}
        else:
            d_res, c_res = float(d.amount_residual), -float(c.amount_residual)
            if d_res <= 0:
                di += 1
                continue
            if c_res <= 0:
                ci += 1
                continue
            amount = float(ccur.round(min(d_res, c_res)))
            vals = {
                "amount": amount,
                "debit_amount_currency": _share(d, amount, dcur, ccur),
                "credit_amount_currency": _share(c, amount, ccur_line, ccur, credit=True),
            }
        partial = partial_model.create(
            {
                "debit_move_id": d.ids[0],
                "credit_move_id": c.ids[0],
                "max_date": max(d.date, c.date),
                **vals,
            }
        )
        created |= partial
        for line in (d, c):
            line.env.cache.invalidate_field(line._fields["matched_credit_ids"], line.ids)
            line.env.cache.invalidate_field(line._fields["matched_debit_ids"], line.ids)
        (d | c)._init_residuals()
        if (same_foreign and d.amount_residual_currency <= 0) or (not same_foreign and d.amount_residual <= 0):
            di += 1
        if (same_foreign and c.amount_residual_currency >= 0) or (not same_foreign and c.amount_residual >= 0):
            ci += 1
    exchange = _exchange_difference(open_lines, created)
    full = _full_reconcile(open_lines | (exchange.line_ids if exchange else open_lines.browse([])))
    moves = (open_lines.mapped("move_id")).filtered(lambda m: m.is_invoice())
    moves._compute_payment_state()
    _notify_payments(open_lines)
    return {"partials": len(created), "full": bool(full), "exchange_move_id": exchange.ids[0] if exchange else None}


def _share(line: Model, amount: float, currency: Model, ccur: Model, *, credit: bool = False) -> float:
    """Currency amount matching ``amount`` (company currency) on ``line``."""
    residual = abs(float(line.amount_residual))
    residual_cur = abs(float(line.amount_residual_currency))
    if currency.ids == ccur.ids:
        return amount
    if residual <= 0:
        return 0.0
    if abs(amount - residual) < 1e-9:
        return residual_cur
    return float(currency.round(residual_cur * amount / residual))


def _exchange_date(company: Model, lines: Model) -> dt.date:
    day = max(lines.mapped("date"))
    lock = company._lock_date_for(adviser=True, tax=False)
    if lock and day <= lock:
        day = lock + dt.timedelta(days=1)
    return day


def _exchange_journal(company: Model) -> Model:
    journal = company.exchange_journal_id
    if not journal:
        journal = (
            company.env["account.journal"]
            .elevated()
            .search([("type", "=", "general"), ("company_id", "=", company.ids[0])], order="sequence, id", limit=1)
        )
    if not journal:
        raise UserError(_("Set an exchange difference journal on the company"))
    return journal


def _exchange_difference(lines: Model, partials: Model) -> Model | None:
    """Post the realised exchange difference for items settled in currency but not in company currency."""
    targets = lines.filtered(
        lambda line: (
            line.amount_residual_currency == 0
            and line.amount_residual != 0
            and line.currency_id
            and line.currency_id.ids != line.company_id.elevated().currency_id.ids
        )
    )
    if not targets:
        return None
    company = targets[0].company_id.elevated()
    gain, loss = company.income_currency_exchange_account_id, company.expense_currency_exchange_account_id
    if not gain or not loss:
        raise UserError(_("Set the exchange gain and loss accounts on the company"))
    env = lines.env
    move_lines: list[tuple[str, dict[str, Any]]] = []
    counter: dict[int, float] = defaultdict(float)
    for line in targets:
        residual = float(line.amount_residual)
        move_lines.append(
            (
                "create",
                {
                    "name": _("Exchange difference: {name}", name=line.move_id.name),
                    "account_id": line.account_id.ids[0],
                    "partner_id": line.partner_id.ids[0] if line.partner_id else None,
                    "balance": -residual,
                    "amount_currency": 0.0,
                    "currency_id": line.currency_id.ids[0],
                    "display_type": "product",
                },
            )
        )
        account = loss if residual > 0 else gain  # the counterpart is a debit (loss) or a credit (gain)
        counter[account.ids[0]] += residual
    ccur = company.currency_id
    for account_id, amount in counter.items():
        move_lines.append(
            (
                "create",
                {
                    "name": _("Exchange difference"),
                    "account_id": account_id,
                    "balance": float(ccur.round(amount)),
                    "amount_currency": float(ccur.round(amount)),
                    "currency_id": ccur.ids[0],
                    "display_type": "product",
                },
            )
        )
    move_model = env["account.move"].elevated("exchange difference")
    move = move_model.create(
        {
            "move_type": "entry",
            "journal_id": _exchange_journal(company).ids[0],
            "company_id": company.ids[0],
            "currency_id": ccur.ids[0],
            "date": _exchange_date(company, targets),
            "ref": _("Realised exchange difference"),
            "line_ids": move_lines,
        }
    )
    move.action_post()
    partial_model = env["account.partial_reconcile"].elevated().with_context(**{INTERNAL: True})
    for line in targets:
        fx_line = move.line_ids.filtered(
            lambda ln, t=line: (
                ln.account_id.ids == t.account_id.ids
                and ln.partner_id.ids == t.partner_id.ids
                and float(ln.balance) == -float(t.amount_residual)
                and not ln.matched_debit_ids
                and not ln.matched_credit_ids
            )
        )[:1]
        debit, credit = (line, fx_line) if float(line.amount_residual) > 0 else (fx_line, line)
        partial_model.create(
            {
                "debit_move_id": debit.ids[0],
                "credit_move_id": credit.ids[0],
                "amount": abs(float(line.amount_residual)),
                "debit_amount_currency": 0.0,
                "credit_amount_currency": 0.0,
                "exchange_move_id": move.ids[0],
                "max_date": move.date,
            }
        )
        for ln in (line, fx_line):
            ln.env.cache.invalidate_field(ln._fields["matched_credit_ids"], ln.ids)
            ln.env.cache.invalidate_field(ln._fields["matched_debit_ids"], ln.ids)
        (line | fx_line)._init_residuals()
    return move


def _connected(lines: Model) -> Model:
    """Every item linked to ``lines`` through partial matches."""
    seen = lines
    frontier = lines
    while frontier:
        nxt = frontier.mapped("matched_debit_ids.debit_move_id") | frontier.mapped("matched_credit_ids.credit_move_id")
        nxt = nxt - seen
        seen |= nxt
        frontier = nxt
    return seen


def _full_reconcile(lines: Model) -> Model | None:
    group = _connected(lines).with_context(**{INTERNAL: True})
    group._init_residuals()
    if not group or any(ln.amount_residual != 0 or ln.amount_residual_currency != 0 for ln in group):
        return None
    if group.filtered(lambda ln: ln.full_reconcile_id):
        old = group.mapped("full_reconcile_id")
        old.elevated().with_context(**{INTERNAL: True}).unlink()
    partials = group.mapped("matched_debit_ids") | group.mapped("matched_credit_ids")
    exchange = partials.mapped("exchange_move_id")[:1]
    full = (
        lines.env["account.full_reconcile"]
        .elevated()
        .create({"name": "/", "exchange_move_id": exchange.ids[0] if exchange else None})
    )
    seq = full.ids[0]
    full.write({"name": f"M{seq:06d}"})
    partials.with_context(**{INTERNAL: True}).write({"full_reconcile_id": full.ids[0]})
    group.write({"full_reconcile_id": full.ids[0], "reconciled": True})
    return full


def _notify_payments(lines: Model) -> None:
    """Payments whose counterpart items changed recompute their matched state (bank module hook)."""
    payments = lines.mapped("payment_id")
    if payments and hasattr(payments, "_compute_reconciliation"):
        payments._compute_reconciliation()


def unreconcile_lines(lines: Model) -> None:
    lines.check_access("write")
    lines = lines.elevated("unreconcile").with_context(**{INTERNAL: True})
    partials = lines.mapped("matched_debit_ids") | lines.mapped("matched_credit_ids")
    if not partials:
        return
    involved = partials.mapped("debit_move_id") | partials.mapped("credit_move_id")
    exchange_moves = partials.mapped("exchange_move_id")
    fulls = partials.mapped("full_reconcile_id") | involved.mapped("full_reconcile_id")
    involved.write({"full_reconcile_id": None, "reconciled": False})
    if fulls:
        fulls.with_context(**{INTERNAL: True}).unlink()
    partials.unlink()
    for line in involved:
        line.env.cache.invalidate_field(line._fields["matched_credit_ids"], line.ids)
        line.env.cache.invalidate_field(line._fields["matched_debit_ids"], line.ids)
    involved._init_residuals()
    for move in exchange_moves.filtered(lambda m: m.state == "posted"):
        # an exchange entry cannot be deleted once numbered: reverse it and match the reversal
        reversal_ids = move.action_reverse(reason=_("unreconciled"), mode="refund")
        reversal = move.browse(reversal_ids)
        reversal.action_post()
        reversal._reconcile_with(move)
    involved.mapped("move_id").filtered(lambda m: m.is_invoice())._compute_payment_state()
    _notify_payments(involved)
