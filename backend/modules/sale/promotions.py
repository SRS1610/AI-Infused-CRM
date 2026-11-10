"""Promotion engine: which promotions an order is eligible for, and the reward lines they give.

* Eligibility: the promotion is active, belongs to the order's company (or to none), today is within its
  dates, it has uses left (confirmed orders other than this one), and one of its conditions holds (or it has
  none). A condition holds when the order's untaxed amount (product lines, before rewards) reaches its minimum
  amount, the quantity of its products reaches its minimum quantity and, when it lists products or categories,
  at least one line has one of them.
* Rewards are ``is_reward_line`` lines. Their taxes follow the lines they discount ("tax treatment"): a
  percentage on the order gives one line per set of taxes of the discounted lines; a fixed amount is split
  over those sets in proportion to their amounts; a percentage on the cheapest line uses that line's taxes; a
  free product is a line of that product at a 100 % discount with its own taxes. Discounts are computed on
  prices as written on the lines (so tax-included prices stay tax-included).
* Applying again updates the existing reward lines in place, creates the missing ones and removes those whose
  promotion is no longer eligible: the result does not depend on how many times it runs.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from modules.sale.models.quotation import INTERNAL, QTY_EPS
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model


def _product_lines(order: Model) -> Model:
    return order.line_ids.filtered(
        lambda line: (
            not line.display_type
            and not line.is_optional
            and not line.is_reward_line
            and not line.is_downpayment
            and (line.quantity or 0) > 0
        )
    )


def _basis(lines: Model) -> float:
    """Sum of quantity x discounted unit price (the amount as written on the lines)."""
    return sum(float(line.quantity or 0.0) * line._price_reduced() for line in lines)


def _to_order_currency(promo: Model, amount: float, order: Model) -> float:
    currency = promo.currency_id
    if not currency or currency.ids == order.currency_id.ids:
        return float(amount)
    return float(currency.convert(amount, order.currency_id, order.company_id, order.env.today()))


def matching_lines(promo: Model, order: Model) -> Model | None:
    """The order lines the promotion's satisfied conditions are about (all product lines when it has no
    condition), or None when the order is not eligible on its conditions."""
    lines = _product_lines(order)
    rules = promo.rule_ids
    if not rules:
        return lines
    untaxed = sum(lines.mapped("price_subtotal"))
    matched = order.env["sale.quotation_line"].elevated().browse([])
    eligible = False
    for rule in rules:
        mine = lines.filtered(lambda line, r=rule: line.product_id and r._matches_product(line.product_id))
        if (rule.product_ids or rule.category_ids) and not mine:
            continue
        if rule.minimum_qty and sum(mine.mapped("quantity")) + QTY_EPS < rule.minimum_qty:
            continue
        if rule.minimum_amount and untaxed + 0.005 < _to_order_currency(promo, rule.minimum_amount, order):
            continue
        eligible = True
        matched |= mine
    return matched if eligible else None


def ineligibility(promo: Model, order: Model) -> str | None:
    """Why ``order`` cannot use ``promo`` (None when it can)."""
    today = order.env.today()
    if not promo.active:
        return _("This promotion is no longer active")
    if promo.company_id and promo.company_id.ids != order.company_id.ids:
        return _("This promotion is not available for this company")
    if promo.date_from and today < promo.date_from:
        return _("This promotion has not started yet")
    if promo.date_to and today > promo.date_to:
        return _("This promotion has ended")
    if promo.max_usage and promo._usage(exclude_order_id=order.ids[0]) >= promo.max_usage:
        return _("This promotion has been used the maximum number of times")
    if matching_lines(promo, order) is None:
        return _("The order does not meet the promotion's conditions")
    return None


def _tax_groups(lines: Model) -> dict[tuple[int, ...], Model]:
    groups: dict[tuple[int, ...], Model] = {}
    for line in lines:
        key = tuple(sorted(line.tax_ids.ids))
        groups[key] = groups[key] | line if key in groups else line
    return groups


def _tax_label(order: Model, tax_ids: tuple[int, ...]) -> str:
    if not tax_ids:
        return ""
    taxes = order.env["account.tax"].elevated().browse(list(tax_ids))
    return ", ".join(t.description or t.name for t in taxes)


def reward_values(promo: Model, order: Model, already: float = 0.0) -> list[dict[str, Any]]:
    """Reward line values of an eligible promotion (without ``quotation_id``). ``already`` is the (negative)
    amount of the rewards given before it: order discounts never take the order below zero."""
    lines = _product_lines(order)
    matched = matching_lines(promo, order) or lines
    out: list[dict[str, Any]] = []
    for reward in promo.reward_ids:
        label = reward.description or promo.name
        base = {
            "is_reward_line": True,
            "promotion_id": promo.ids[0],
            "promotion_reward_id": reward.ids[0],
            "quantity": 1.0,
            "price_manual": True,
            "sequence": 9000 + len(out),
        }
        if reward.reward_type in ("discount_order", "fixed_amount"):
            groups = _tax_groups(lines)
            total = _basis(lines)
            remaining = total + already + sum(_value(v) for v in out)
            if not groups or total <= 0 or remaining <= 0:
                continue
            pct = float(reward.discount_percent or 0.0) / 100.0
            if reward.reward_type == "fixed_amount":
                pct = _to_order_currency(promo, reward.fixed_amount or 0.0, order) / total
            pct = min(pct, remaining / total)
            for key, group in groups.items():
                value = -float(order.currency_id.round(_basis(group) * pct))
                if not value:
                    continue
                suffix = f" ({_tax_label(order, key)})" if len(groups) > 1 and key else ""
                name = (
                    f"{label}: {float(reward.discount_percent or 0):g} %{suffix}"
                    if reward.reward_type == "discount_order"
                    else f"{label}{suffix}"
                )
                out.append({**base, "name": name, "price_unit": value, "tax_ids": [("set", list(key))]})
        elif reward.reward_type == "discount_cheapest":
            candidates = matched.filtered(lambda line: line._price_reduced() > 0)
            if not candidates:
                continue
            cheapest = candidates.sorted(lambda line: (float(line.quantity or 0) * line._price_reduced(), line.ids[0]))[
                0
            ]
            value = -float(order.currency_id.round(_basis(cheapest) * float(reward.discount_percent or 0.0) / 100.0))
            out.append(
                {
                    **base,
                    "name": f"{label}: {float(reward.discount_percent or 0):g} % ({cheapest.product_id.name})",
                    "price_unit": value,
                    "tax_ids": [("set", sorted(cheapest.tax_ids.ids))],
                }
            )
        elif reward.reward_type == "free_product" and reward.free_product_id:
            product = reward.free_product_id
            out.append(
                {
                    **base,
                    "name": f"{label}: {product.name}",
                    "product_id": product.ids[0],
                    "quantity": float(reward.free_quantity or 1.0),
                    "uom_id": product.uom_id.ids[0] if product.uom_id else None,
                    "price_unit": order._price_for(product, float(reward.free_quantity or 1.0)),
                    "discount": 100.0,
                    "tax_ids": [("set", sorted(order._taxes_for_product(product).ids))],
                }
            )
    return out


def _value(vals: dict[str, Any]) -> float:
    """Amount of reward line values as written (negative for discounts)."""
    return (
        float(vals["price_unit"]) * float(vals.get("quantity") or 0.0) * (1 - float(vals.get("discount") or 0.0) / 100)
    )


def _key(vals_or_line: Any) -> tuple[Any, ...]:
    if isinstance(vals_or_line, dict):
        taxes = tuple(sorted(vals_or_line["tax_ids"][0][1]))
        return (
            vals_or_line["promotion_id"],
            vals_or_line["promotion_reward_id"],
            taxes,
            vals_or_line.get("product_id"),
        )
    line = vals_or_line
    return (
        line.promotion_id.ids[0] if line.promotion_id else None,
        line.promotion_reward_id.ids[0] if line.promotion_reward_id else None,
        tuple(sorted(line.tax_ids.ids)),
        line.product_id.ids[0] if line.product_id else None,
    )


def find_code(order: Model, code: str) -> Model:
    promos = order.env["sale.promotion"].elevated()
    found = promos.search(
        [
            ("program_type", "=", "code"),
            ("code", "=", code.strip().upper()),
            {"any": [("company_id", "is_not_set"), ("company_id", "=", order.company_id.ids[0])]},
        ],
        limit=1,
    )
    if not found:
        raise UserError(_("Unknown promotion code '{code}'", code=code.strip()))
    return found


def candidates(order: Model) -> Model:
    """Automatic promotions of the order's company and the code promotions entered on the order."""
    promos = order.env["sale.promotion"].elevated()
    auto = promos.search(
        [
            ("program_type", "=", "automatic"),
            {"any": [("company_id", "is_not_set"), ("company_id", "=", order.company_id.ids[0])]},
        ]
    )
    coded = promos.browse(order.elevated().promotion_code_ids.ids).filtered(lambda p: p.active)
    return (auto | coded).sorted(lambda p: (p.sequence or 0, p.ids[0]))


def apply_promotions(orders: Model, code: str | None = None) -> dict[str, Any]:
    applied: set[int] = set()
    for order in orders:
        order.check_access("write")
        if order.locked:
            raise UserError(_("Sales order {name} is locked: unlock it to change it", name=order.name))
        if order.state not in ("draft", "sent", "accepted"):
            raise UserError(_("Promotions apply to quotations and sales orders only"))
        sys_order = order.elevated("promotions")
        rewards = sys_order.line_ids.filtered(lambda line: line.is_reward_line)
        if rewards.filtered(lambda line: abs(line.qty_invoiced or 0.0) > QTY_EPS):
            raise UserError(_("The promotions of {name} are already invoiced", name=order.name))
        if code:
            promo = find_code(sys_order, code)
            reason = ineligibility(promo, sys_order)
            if reason:
                raise UserError(reason)
            if promo.ids[0] not in sys_order.promotion_code_ids.ids:
                sys_order.with_context(**{INTERNAL: True}).write({"promotion_code_ids": [("add", promo.ids[0])]})
        wanted: list[dict[str, Any]] = []
        for promo in candidates(sys_order):
            if ineligibility(promo, sys_order) is None:
                wanted.extend(reward_values(promo, sys_order, sum(_value(v) for v in wanted)))
        existing: dict[tuple[Any, ...], list[Model]] = defaultdict(list)
        for line in rewards:
            existing[_key(line)].append(line)
        lines = order.env["sale.quotation_line"].with_context(**{INTERNAL: True, "sale_no_suggestions": True})
        keep: list[int] = []
        for vals in wanted:
            match = existing.get(_key(vals))
            if match:
                line = match.pop(0)
                updates = {
                    k: v
                    for k, v in vals.items()
                    if k not in ("tax_ids", "promotion_id", "promotion_reward_id", "is_reward_line", "price_manual")
                    and _differs(line, k, v)
                }
                if updates:
                    line.with_context(**{INTERNAL: True}).write(updates)
                keep.append(line.ids[0])
            else:
                keep.extend(lines.create({**vals, "quotation_id": order.ids[0]}).ids)
            applied.add(vals["promotion_id"])
        stale = rewards.browse([i for i in rewards.ids if i not in keep])
        if stale:
            stale.with_context(**{INTERNAL: True}).unlink()
    return {"promotion_ids": sorted(applied)}


def _differs(line: Model, name: str, value: Any) -> bool:
    current = line[name]
    if isinstance(current, Model):
        return (current.ids[0] if current else None) != value
    if isinstance(current, float) or isinstance(value, float):
        return abs(float(current or 0.0) - float(value or 0.0)) > 1e-9
    return bool(current != value)
