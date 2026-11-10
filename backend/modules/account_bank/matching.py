"""Matching bank transactions with journal items (the ledger side of bank reconciliation).

A posted transaction's entry is ``bank account ↔ suspense``. Matching rebuilds that entry through the
ledger's own rules: the entry is reset to draft (lock dates apply), the suspense item is replaced by one
counterpart per matched item (on the item's account, partner and currency) and by the write-off lines, any
amount left unexplained stays on the suspense account, the entry is posted again (balance check, number
kept) and every counterpart is reconciled with its item (``reconcile_lines``: partial matches leave the
remainder open, foreign-currency items get their realised exchange difference). Undoing restores the single
suspense item.

Adding matches to a partly matched transaction re-applies the previous matches (kept in ``match_data``)
together with the new ones.
"""

from __future__ import annotations

from typing import Any

from modules.account.models.move import INTERNAL
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model

TOLERANCE = 0.005


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


def _split(st_line: Model) -> tuple[Model, Model]:
    """(liquidity item, other items) of a transaction's entry."""
    move = st_line.move_id
    bank_account = st_line.journal_id.elevated().default_account_id
    lines = move.line_ids.sorted(lambda r: r.ids[0])
    liquidity = lines.filtered(lambda r: r.account_id.ids == bank_account.ids)[:1]
    if not liquidity:
        raise UserError(_("The entry of this transaction has no bank item"))
    return liquidity, lines - liquidity


def _target_lines(st_line: Model, items: list[dict[str, Any]]) -> list[tuple[Model, float | None]]:
    env = st_line.env
    company = st_line.company_id
    suspense = st_line.journal_id._bank_suspense_account()
    out: list[tuple[Model, float | None]] = []
    seen: set[int] = set()
    for spec in items:
        line_id = int(spec["line_id"])
        if line_id in seen:
            continue
        seen.add(line_id)
        target = env["account.move_line"].browse([line_id]).exists()
        if not target:
            raise UserError(_("Journal item {id} does not exist", id=line_id))
        account = target.account_id
        if target.company_id.ids != company.ids:
            raise UserError(_("Items of another company cannot be matched"))
        if target.parent_state != "posted" or not account.reconcile or target.reconciled:
            raise UserError(_("{name} is not an open item", name=target.display_name))
        if target.move_id.ids == st_line.move_id.ids or account.ids == suspense.ids:
            raise UserError(_("{name} cannot be matched with this transaction", name=target.display_name))
        amount = spec.get("amount")
        out.append((target, abs(float(amount)) if amount not in (None, "") else None))
    return out


def _writeoff_lines(
    st_line: Model, spec: dict[str, Any], balance: float, ccur: Model, jcur: Model, open_c: float, open_j: float
) -> list[dict[str, Any]]:
    """Counterpart items of one write-off (``balance`` signed as booked; with a tax the amount includes it)."""
    env = st_line.env
    account = env["account.account"].browse([int(spec["account_id"])]).exists()
    if not account or account.company_id.ids != st_line.company_id.ids:
        raise UserError(_("Choose a write-off account of the transaction's company"))
    label = spec.get("label") or _("Write-off")
    partner_id = st_line.partner_id.ids[0] if st_line.partner_id else None

    def cur_amount(value: float) -> float:
        if jcur.ids == ccur.ids or not open_c:
            return value
        return float(jcur.round(open_j * value / open_c))

    tax = env["account.tax"].browse([int(spec["tax_id"])]).exists() if spec.get("tax_id") else None
    if not tax:
        return [
            {
                "name": label,
                "account_id": account.ids[0],
                "partner_id": partner_id,
                "balance": balance,
                "amount_currency": cur_amount(balance),
                "currency_id": jcur.ids[0],
            }
        ]
    rate = sum(float(t.amount or 0) for t in tax._flatten() if t.amount_type == "percent")
    base_guess = abs(balance) / (1 + rate / 100.0)
    res = tax.compute_all(base_guess, ccur, 1.0)
    sign = _sign(balance)
    tax_total = 0.0
    out: list[dict[str, Any]] = []
    for t in res["taxes"]:
        amount = sign * float(t["amount"])
        if not amount:
            continue
        tax_total += amount
        out.append(
            {
                "name": f"{label} ({t['name']})",
                "account_id": t["account_id"] or account.ids[0],
                "partner_id": partner_id,
                "balance": amount,
                "amount_currency": cur_amount(amount),
                "currency_id": jcur.ids[0],
                "tax_line_id": t["id"],
                "tax_repartition_line_id": t["tax_repartition_line_id"],
                "tax_tag_ids": [("set", t["tag_ids"])],
                "tax_base_amount": float(ccur.round(base_guess)),
            }
        )
    base = float(ccur.round(balance - tax_total))
    out.insert(
        0,
        {
            "name": label,
            "account_id": account.ids[0],
            "partner_id": partner_id,
            "balance": base,
            "amount_currency": cur_amount(base),
            "currency_id": jcur.ids[0],
            "tax_ids": [("set", tax.ids)],
            "tax_tag_ids": [("set", res["base_tags"])],
        },
    )
    return out


def _plan(
    st_line: Model, items: list[dict[str, Any]], writeoffs: list[dict[str, Any]]
) -> tuple[list[tuple[dict[str, Any], Model | None]], dict[str, Any]]:
    """New counterpart items [(values, target or None)] and the resolved specs to remember."""
    journal = st_line.journal_id.elevated()
    company = journal.company_id
    ccur = company.currency_id
    jcur = journal._bank_currency()
    liquidity, _others = _split(st_line)
    total_c, total_j = float(liquidity.balance), float(liquidity.amount_currency)
    open_c, open_j = total_c, total_j
    day = st_line.date
    planned: list[tuple[dict[str, Any], Model | None]] = []
    resolved: dict[str, Any] = {"items": [], "writeoffs": []}

    def j_of(company_amount: float) -> float:
        if jcur.ids == ccur.ids:
            return company_amount
        return float(jcur.round(total_j * company_amount / total_c)) if total_c else 0.0

    for target, wanted in _target_lines(st_line, items):
        tcur = target.currency_id or ccur
        r_c, r_t = float(target.amount_residual), float(target.amount_residual_currency)
        if tcur.ids == ccur.ids:
            r_t = r_c
        open_t = (
            open_c
            if tcur.ids == ccur.ids
            else open_j
            if tcur.ids == jcur.ids
            else ccur.convert(open_c, tcur, company, day)
        )
        consumes = _sign(r_t) == _sign(open_t) and open_t != 0
        if wanted is not None:
            alloc_t = _sign(r_t) * min(wanted, abs(r_t))
        elif consumes:
            alloc_t = _sign(r_t) * min(abs(r_t), abs(open_t))
        elif _sign(r_t) != _sign(open_t) and open_t != 0:
            alloc_t = r_t
        else:
            continue  # nothing left to explain
        alloc_t = float(tcur.round(alloc_t))
        if not alloc_t:
            continue
        if tcur.ids == ccur.ids:
            alloc_c = alloc_t
        elif consumes and abs(alloc_t) >= abs(open_t) - TOLERANCE:
            alloc_c = open_c  # the whole remaining bank amount: the rate difference becomes an exchange difference
        elif tcur.ids == jcur.ids and total_j:
            alloc_c = float(ccur.round(total_c * alloc_t / total_j))
        else:
            alloc_c = float(tcur.convert(alloc_t, ccur, company, day))
        alloc_j = alloc_t if tcur.ids == jcur.ids else j_of(alloc_c)
        open_c = float(ccur.round(open_c - alloc_c))
        open_j = float(jcur.round(open_j - alloc_j))
        move = target.move_id
        planned.append(
            (
                {
                    "name": move.name if move.name and move.name != "/" else (target.name or ""),
                    "account_id": target.account_id.ids[0],
                    "partner_id": target.partner_id.ids[0] if target.partner_id else None,
                    "balance": -alloc_c,
                    "amount_currency": -alloc_t,
                    "currency_id": tcur.ids[0],
                    "date_maturity": target.date_maturity,
                },
                target,
            )
        )
        resolved["items"].append({"line_id": target.ids[0], "amount": abs(alloc_t)})
    for spec in writeoffs:
        balance = spec.get("balance")
        value = -open_c if balance in (None, "") else float(ccur.round(float(balance)))
        if not value:
            continue
        lines = _writeoff_lines(st_line, spec, value, ccur, jcur, open_c, open_j)
        for vals in lines:
            planned.append((vals, None))
            open_c = float(ccur.round(open_c + vals["balance"]))
            open_j = float(jcur.round(open_j + vals["amount_currency"]))
        resolved["writeoffs"].append(
            {
                "account_id": int(spec["account_id"]),
                "balance": value,
                "label": spec.get("label"),
                "tax_id": spec.get("tax_id"),
            }
        )
    if abs(open_c) > TOLERANCE or abs(open_j) > TOLERANCE:
        planned.append((_suspense_values(st_line, -open_c, -open_j), None))
    resolved["open"] = open_j
    return planned, resolved


def _suspense_values(st_line: Model, balance: float, amount_currency: float) -> dict[str, Any]:
    journal = st_line.journal_id.elevated()
    return {
        "name": st_line.payment_ref or st_line.ref or _("Bank transaction"),
        "account_id": journal._bank_suspense_account().ids[0],
        "partner_id": st_line.partner_id.ids[0] if st_line.partner_id else None,
        "balance": balance,
        "amount_currency": amount_currency,
        "currency_id": journal._bank_currency().ids[0],
    }


def _rebuild(st_line: Model, planned: list[tuple[dict[str, Any], Model | None]]) -> list[tuple[Model, Model]]:
    """Replace the entry's non-bank items through draft → post; returns (counterpart, target) pairs."""
    move = st_line.move_id
    _liquidity, others = _split(st_line)
    if others.filtered(lambda r: r.matched_debit_ids or r.matched_credit_ids):
        raise UserError(_("Undo the transaction's current matches first"))
    move.button_draft()
    others.with_context(**{INTERNAL: True}).unlink()
    line_model = st_line.env["account.move_line"].with_context(**{INTERNAL: True})
    pairs: list[tuple[Model, Model]] = []
    for vals, target in planned:
        created = line_model.create({**vals, "move_id": move.ids[0]})
        if target is not None:
            pairs.append((created, target))
    move.action_post()
    return pairs


def match_line(
    st_line: Model, items: list[dict[str, Any]], writeoffs: list[dict[str, Any]], *, model: Model | None = None
) -> dict[str, Any]:
    st_line = st_line.elevated("bank reconciliation")
    if st_line.state == "draft":
        st_line._post()
    if not items and not writeoffs:
        raise UserError(_("Choose items to match or a write-off"))
    previous = st_line.match_data or {}
    if previous.get("items") or previous.get("writeoffs"):
        new_ids = {int(i["line_id"]) for i in items}
        items = [i for i in previous.get("items", []) if int(i["line_id"]) not in new_ids] + list(items)
        writeoffs = list(previous.get("writeoffs", [])) + list(writeoffs)
        unmatch_line(st_line)
    planned, resolved = _plan(st_line, items, writeoffs)
    pairs = _rebuild(st_line, planned)
    for counterpart, target in pairs:
        (counterpart | target).reconcile()
    open_j = float(resolved.pop("open"))
    reconciled = abs(open_j) <= TOLERANCE
    st_line.with_context(_bank_internal=True).write(
        {
            "state": "reconciled" if reconciled else "posted",
            "is_reconciled": reconciled,
            "amount_residual": open_j,
            "match_data": resolved,
            "reconcile_model_id": model.ids[0] if model else None,
        }
    )
    return {"state": st_line.state, "amount_residual": open_j, "matched": len(pairs)}


def unmatch_line(st_line: Model) -> None:
    st_line = st_line.elevated("bank reconciliation")
    if not st_line.move_id:
        return
    liquidity, others = _split(st_line)
    matched = others.filtered(lambda r: r.matched_debit_ids or r.matched_credit_ids)
    if matched:
        matched.remove_move_reconcile()
    suspense = st_line.journal_id._bank_suspense_account()
    if not (len(others) == 1 and others.account_id.ids == suspense.ids):
        _rebuild(
            st_line, [(_suspense_values(st_line, -float(liquidity.balance), -float(liquidity.amount_currency)), None)]
        )
    st_line.with_context(_bank_internal=True).write(
        {
            "state": "posted",
            "is_reconciled": False,
            "amount_residual": st_line.amount,
            "match_data": None,
            "reconcile_model_id": None,
        }
    )


def line_info(st_line: Model, *, brief: bool = False) -> dict[str, Any]:
    """The transaction as the reconciliation screen shows it."""
    currency = st_line.currency_id
    info: dict[str, Any] = {
        "id": st_line.ids[0],
        "date": st_line.date.isoformat() if st_line.date else None,
        "payment_ref": st_line.payment_ref,
        "ref": st_line.ref,
        "partner": {"id": st_line.partner_id.ids[0], "name": st_line.partner_id.display_name}
        if st_line.partner_id
        else None,
        "partner_name": st_line.partner_name,
        "account_number": st_line.account_number,
        "amount": float(st_line.amount or 0),
        "amount_residual": float(st_line.amount_residual or 0),
        "currency": currency.name if currency else None,
        "state": st_line.state,
        "journal": st_line.journal_id.display_name,
        "statement": st_line.statement_id.name if st_line.statement_id else None,
        "rule": st_line.reconcile_model_id.name if st_line.reconcile_model_id else None,
    }
    if brief:
        return info
    info["narration"] = st_line.narration
    info["foreign_currency"] = st_line.foreign_currency_id.name if st_line.foreign_currency_id else None
    info["amount_currency"] = float(st_line.amount_currency or 0)
    counterparts: list[dict[str, Any]] = []
    if st_line.move_id:
        sys_line = st_line.elevated()
        _liq, others = _split(sys_line)
        for item in others:
            targets = item._reconciled_counterparts().filtered(
                lambda r: not r.move_id.statement_line_id or r.move_id.statement_line_id.ids != st_line.ids
            )
            counterparts.append(
                {
                    "id": item.ids[0],
                    "account": item.account_id.display_name,
                    "label": item.name,
                    "partner": item.partner_id.display_name if item.partner_id else None,
                    "balance": float(item.balance),
                    "suspense": item.account_id.ids == sys_line.journal_id._bank_suspense_account().ids,
                    "matched": [{"id": t.ids[0], "move": t.move_id.name, "move_id": t.move_id.ids[0]} for t in targets],
                }
            )
    info["counterparts"] = counterparts
    return info
