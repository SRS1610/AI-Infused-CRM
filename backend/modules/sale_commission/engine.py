"""Commission computation.

For a salesperson *P*, a plan and a period [*from*, *to*]:

1. **Source lines.** Product lines of posted customer invoices and credit notes of the plan's company whose
   salesperson is *P*: the invoice's *Salesperson* (``invoice_user_id``, filled from the sales order's salesperson
   and still editable on posted invoices), and for a credit note made from an invoice the original invoice's
   salesperson. A line counts on a date:

   * basis *invoiced*: the accounting date of the invoice or credit note (credit notes reduce the commission of the
     period they are posted in);
   * basis *paid*: an invoice counts on the date it became fully paid (the latest date of the payments matched with
     it); a credit note counts on its own date when the invoice it credits was fully paid by then (the commission
     was earned), or when it credits no invoice; otherwise it is left out (its invoice never earned commission).

   A line is counted when that date is in the period and *P* is assigned to the plan on that date (directly or
   through a sales team).
2. **Base** = the line's untaxed amount in company currency (``-balance``: positive for invoices, negative for
   credit notes).
3. **Rate.** The most specific rule wins: a rule for the product, else for its category or the nearest parent
   category, else an *all products* rule; equal rules: the lowest sequence. Without a rule the line earns nothing
   (it still counts in the base for the target). *raw* = base x rate / 100 + sign x quantity x fixed amount per
   unit (sign -1 for credit notes).
4. **Achievement** = Σ base / target x 100 (target: the assignment's own target, else the plan's). The
   accelerator applied is the band with the highest *from achievement %* not above the achievement (bands are
   inclusive at their lower bound: exactly 100 % reaches the 100 % band); no band, or no target: x 1. The multiplier
   applies to the whole period (retroactive), not only to the part above the threshold.
5. **Amounts.** Each line's commission = round(raw x multiplier, 2) (half away from zero, exact decimal
   arithmetic); the statement's commission is the sum of its lines; total = commission + adjustments.

Every step uses ``Decimal`` so the result can be checked by hand or in a spreadsheet
(``tests/data/commission_crosscheck.csv``).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from tessera.orm import Model

CENT = Decimal("0.01")


def d(value: Any) -> Decimal:
    return Decimal(str(value or 0))


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass
class Source:
    item: Model  # account.move_line
    move: Model
    day: dt.date
    sign: int


def salesperson(move: Model) -> Model:
    """Who earns the commission of ``move``: its salesperson, or for a credit note of an invoice the invoice's."""
    if move.move_type == "out_refund" and move.reversed_entry_id:
        return move.reversed_entry_id.invoice_user_id
    return move.invoice_user_id


def paid_date(move: Model) -> dt.date | None:
    """When ``move`` became fully paid (latest matching of its receivable items), None when it is not paid."""
    if move.payment_state != "paid":
        return None
    days: list[dt.date] = []
    for item in move.line_ids.filtered(lambda r: r.display_type == "payment_term"):
        for partial in item.matched_credit_ids | item.matched_debit_ids:
            if partial.max_date:
                days.append(partial.max_date)
    return max(days) if days else move.date


def _counted_on(move: Model, basis: str) -> dt.date | None:
    if basis == "invoiced":
        return move.date
    if move.move_type == "out_invoice":
        return paid_date(move)
    original = move.reversed_entry_id
    if not original:
        return move.date
    paid = paid_date(original)
    return move.date if paid and paid <= move.date else None


def source_lines(plan: Model, user: Model, date_from: dt.date, date_to: dt.date) -> list[Source]:
    """The invoice and credit note lines earning commission for ``user`` in the period (see the module docstring)."""
    env = plan.env
    plan = plan.elevated()
    uid = user.ids[0]
    moves = env["account.move"].elevated("commission statement")
    domain: list[Any] = [
        ("company_id", "=", plan.company_id.ids[0]),
        ("state", "=", "posted"),
        ("move_type", "in", ["out_invoice", "out_refund"]),
        {"any": [["invoice_user_id", "=", uid], ["reversed_entry_id.invoice_user_id", "=", uid]]},
    ]
    if plan.basis == "invoiced":
        domain += [("date", ">=", date_from), ("date", "<=", date_to)]
    else:  # paid on or before the end of the period; posted credit notes of the period
        domain.append(("date", "<=", date_to))
    assignments = plan._assignments(date_from, date_to)
    out: list[Source] = []
    for move in moves.search(domain, order="date, name, id"):
        if salesperson(move).ids != [uid]:
            continue
        day = _counted_on(move, plan.basis)
        if day is None or not (date_from <= day <= date_to):
            continue
        if not any(a.covers(uid, day) for a in assignments):
            continue
        sign = -1 if move.move_type == "out_refund" else 1
        for item in move.invoice_line_ids.filtered(lambda r: r.display_type == "product"):
            out.append(Source(item, move, day, sign))
    return out


def _category_chain(category: Model) -> list[int]:
    chain: list[int] = []
    cur = category
    while cur and cur.ids[0] not in chain:
        chain.append(cur.ids[0])
        cur = cur.parent_id
    return chain


def rule_for(rules: Model, product: Model) -> Model | None:
    """The most specific rule of ``rules`` for ``product`` (product, nearest category, all)."""
    ordered = rules.sorted(lambda r: (r.sequence or 0, r.ids[0]))
    if product:
        for rule in ordered:
            if rule.applies_to == "product" and rule.product_id.ids == product.ids:
                return rule
        chain = _category_chain(product.elevated().category_id) if product.category_id else []
        for cat_id in chain:
            for rule in ordered:
                if rule.applies_to == "category" and rule.category_id.ids == [cat_id]:
                    return rule
    for rule in ordered:
        if rule.applies_to == "all":
            return rule
    return None


def target_for(plan: Model, user: Model, date_from: dt.date, date_to: dt.date) -> Decimal:
    for assignment in plan._assignments(date_from, date_to).sorted(lambda a: a.date_from or dt.date.min, reverse=True):
        if user.ids[0] in assignment._user_ids() and assignment.target_amount:
            return d(assignment.target_amount)
    return d(plan.target_amount)


def multiplier_for(plan: Model, achievement: Decimal | None) -> tuple[Decimal, Model | None]:
    if achievement is None:
        return Decimal(1), None
    band = None
    for target in plan.target_ids.sorted(lambda t: t.min_achievement):
        if d(target.min_achievement) <= achievement:
            band = target
    return (d(band.multiplier), band) if band is not None else (Decimal(1), None)


def compute(plan: Model, user: Model, date_from: dt.date, date_to: dt.date) -> dict[str, Any]:
    """Statement values and line values for ``user`` (no database writes)."""
    plan = plan.elevated()
    sources = source_lines(plan, user, date_from, date_to)
    rules = plan.rule_ids
    pre: list[dict[str, Any]] = []
    base_total = Decimal(0)
    for src in sources:
        item = src.item
        base = -d(item.balance)
        qty = d(item.quantity)
        product = item.product_id
        rule = rule_for(rules, product)
        rate = d(rule.rate) if rule else Decimal(0)
        fixed = d(rule.fixed_amount) if rule else Decimal(0)
        raw = base * rate / Decimal(100) + src.sign * qty * fixed
        base_total += base
        pre.append(
            {
                "move_line_id": item.ids[0],
                "move_id": src.move.ids[0],
                "move_type": src.move.move_type,
                "date": src.day,
                "partner_id": src.move.partner_id.ids[0] if src.move.partner_id else None,
                "product_id": product.ids[0] if product else None,
                "category_id": product.category_id.ids[0] if product and product.category_id else None,
                "rule_id": rule.ids[0] if rule else None,
                "name": item.name,
                "quantity": float(qty),
                "base_amount": float(base),
                "rate": float(rate),
                "fixed_amount": float(fixed),
                "_raw": raw,
            }
        )
    target = target_for(plan, user, date_from, date_to)
    achievement = (base_total / target * Decimal(100)) if target > 0 else None
    multiplier, _band = multiplier_for(plan, achievement)
    lines = []
    commission = raw_total = Decimal(0)
    for vals in pre:
        raw = vals.pop("_raw")
        amount = money(raw * multiplier)
        commission += amount
        raw_total += money(raw)
        lines.append(
            {**vals, "raw_amount": float(money(raw)), "multiplier": float(multiplier), "amount": float(amount)}
        )
    return {
        "lines": lines,
        "target_amount": float(target),
        "achievement": float(achievement.quantize(CENT, rounding=ROUND_HALF_UP)) if achievement is not None else 0.0,
        "multiplier": float(multiplier),
        "base_amount": float(money(base_total)),
        "raw_amount": float(raw_total),
        "commission_amount": float(commission),
    }


def compute_statement(statement: Model) -> Model:
    """Replace a draft statement's lines and totals by a fresh computation (idempotent)."""
    rec = statement.elevated("commission computation")
    result = compute(rec.plan_id, rec.user_id, rec.date_from, rec.date_to)
    rec.line_ids.unlink()
    lines = [("create", vals) for vals in result.pop("lines")]
    rec.with_context(commission_internal=True).write({**result, "line_ids": lines, "computed_at": rec.env.now()})
    return statement


def generate_statements(plan: Model, day: dt.date) -> Model:
    """Statements of the period containing ``day`` for every salesperson of ``plan`` who has something counted
    in it (drafts recomputed, confirmed and paid ones left alone)."""
    plan.check_access("read")
    statements = plan.env["sale.commission_statement"]
    statements.check_access_rights("create")
    date_from, date_to = plan.period_of(day)
    out = statements.browse([])
    for user in plan.assigned_users(date_from, date_to):
        existing = statements.elevated().search(
            [("plan_id", "=", plan.ids[0]), ("user_id", "=", user.ids[0]), ("date_from", "=", date_from)], limit=1
        )
        if not existing and not source_lines(plan, user, date_from, date_to):
            continue  # nothing earned or credited: no empty statement
        rec = (
            statements.browse(existing.ids)
            if existing
            else statements.create(
                {"plan_id": plan.ids[0], "user_id": user.ids[0], "date_from": date_from, "date_to": date_to}
            )
        )
        if rec.state == "draft":
            compute_statement(rec)
        out |= rec
    return out


def statement_facts(statement: Model) -> dict[str, Any]:
    """The numbers of a statement, structured: what the 2.6 explanation may cite (and nothing else)."""
    rec = statement.elevated()
    rec.ensure_one()
    by_category: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"base": Decimal(0), "commission": Decimal(0), "lines": 0}
    )
    by_rule: dict[str, dict[str, Any]] = {}
    refunds = {"count": 0, "base": Decimal(0), "commission": Decimal(0)}
    moves: set[int] = set()
    for line in rec.line_ids:
        cat = line.category_id.complete_name if line.category_id else ""
        bucket = by_category[cat or "-"]
        bucket["base"] += d(line.base_amount)
        bucket["commission"] += d(line.amount)
        bucket["lines"] += 1
        if line.rule_id:
            key = str(line.rule_id.ids[0])
            by_rule.setdefault(
                key,
                {
                    "applies_to": line.rule_id.applies_to,
                    "target": line.rule_id.describe(),
                    "rate": line.rate,
                    "fixed_per_unit": line.fixed_amount,
                },
            )
        if line.move_type == "out_refund":
            refunds["count"] += 1
            refunds["base"] += d(line.base_amount)
            refunds["commission"] += d(line.amount)
        if line.move_id:
            moves.add(line.move_id.ids[0])
    band = None
    plan = rec.plan_id
    if rec.target_amount:
        _mult, target = multiplier_for(plan, d(rec.achievement))
        if target is not None:
            band = {"from_achievement": target.min_achievement, "multiplier": target.multiplier}
    return {
        "statement_id": rec.ids[0],
        "statement": rec.name,
        "salesperson": rec.user_id.name,
        "plan": plan.name,
        "period": rec.period_label,
        "date_from": rec.date_from.isoformat(),
        "date_to": rec.date_to.isoformat(),
        "basis": rec.basis,
        "state": rec.state,
        "currency": rec.currency_id.name,
        "target": rec.target_amount,
        "achievement_percent": rec.achievement,
        "band": band,
        "bands": [{"from_achievement": t.min_achievement, "multiplier": t.multiplier} for t in plan.target_ids],
        "multiplier": rec.multiplier,
        "base_total": rec.base_amount,
        "commission_before_multiplier": rec.raw_amount,
        "commission": rec.commission_amount,
        "adjustments": rec.adjustment_amount,
        "adjustment_note": rec.adjustment_note or "",
        "total": rec.total_amount,
        "documents": len(moves),
        "lines": len(rec.line_ids),
        "credit_notes": {k: (float(v) if isinstance(v, Decimal) else v) for k, v in refunds.items()},
        "by_category": [
            {"category": k, "base": float(v["base"]), "commission": float(v["commission"]), "lines": v["lines"]}
            for k, v in sorted(by_category.items())
        ],
        "rates": list(by_rule.values()),
    }
