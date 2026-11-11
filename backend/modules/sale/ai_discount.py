"""``sale.discount_suggest`` — a discount per quotation line, always inside the line's approved band.

For each product line the most specific ``sale.discount_band`` (category, customer segment, quantity) gives the
range. Inside it the engine picks the discount with the best expected margin: a logistic model of *won* (sales
order) vs *lost* (rejected or expired) on discount and quantity, fitted on the company's decided quotations of
the same product category, times the line's margin at that discount. With too little history it falls back to
a quantity ramp across the band. The result is rounded to half a point and **clamped to the band**, then
checked again by :func:`within_band` before any proposal is made — a suggestion outside its band cannot exist.

Each suggestion is a write proposal on its line (``discount``), so accepting, editing or rejecting is recorded
per suggestion and :func:`acceptance` gives the acceptance rate. Lines without a band get no suggestion.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modules.sale import ai_common as AC
from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "sale.discount_suggest"
STEP = 0.5
MIN_HISTORY = 8
EPS = 1e-9


@dataclass
class WinModel:
    """P(won) = sigmoid(a + b * discount/100 + c * log1p(quantity))."""

    a: float
    b: float
    c: float
    rows: int
    won: int

    def p(self, discount: float, quantity: float) -> float:
        z = self.a + self.b * discount / 100.0 + self.c * math.log1p(max(0.0, quantity))
        return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))


def fit_win_model(rows: list[tuple[float, float, int]], iterations: int = 400) -> WinModel | None:
    """Logistic regression (L2, gradient descent) on (discount %, quantity, won); None without enough
    history of both outcomes."""
    won = sum(r[2] for r in rows)
    if len(rows) < MIN_HISTORY or won == 0 or won == len(rows):
        return None
    xs = [(d / 100.0, math.log1p(max(0.0, q))) for d, q, _ in rows]
    mean_q = sum(x[1] for x in xs) / len(xs)
    a = b = c = 0.0
    lr, l2 = 0.5, 0.01
    for _ in range(iterations):
        ga = gb = gc = 0.0
        for (x1, x2), (_d, _q, y) in zip(xs, rows, strict=True):
            z = a + b * x1 + c * (x2 - mean_q)
            p = 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, z))))
            err = p - y
            ga += err
            gb += err * x1
            gc += err * (x2 - mean_q)
        n = len(rows)
        a -= lr * ga / n
        b -= lr * (gb / n + l2 * b)
        c -= lr * (gc / n + l2 * c)
    return WinModel(a - c * mean_q, b, c, len(rows), won)


def round_to_step(value: float) -> float:
    return round(round(value / STEP) * STEP, 4)


def clamp_to_band(value: float, low: float, high: float) -> float:
    """Rounded to half a point, then forced into [low, high] (the band's own bounds win over rounding)."""
    return float(min(max(round_to_step(value), low), high))


def within_band(value: float, low: float, high: float) -> bool:
    return low - EPS <= float(value) <= high + EPS


def choose_discount(
    low: float, high: float, quantity: float, price: float, cost: float, model: WinModel | None
) -> tuple[float, str]:
    """(discount %, method) inside [low, high]."""
    if high <= low + EPS:
        return clamp_to_band(low, low, high), "band"
    if model is None:
        ramp = min(1.0, math.log10(max(1.0, quantity)) / 2.0)  # 1 → bottom of the band, 100+ → top
        return clamp_to_band(low + (high - low) * ramp, low, high), "quantity"
    best, best_value = low, -math.inf
    d = low
    while d <= high + EPS:
        net = price * (1 - d / 100.0)
        margin = net - cost if cost > 0 else net
        value = model.p(d, quantity) * margin
        if value > best_value + 1e-9:
            best, best_value = d, value
        d += STEP
    return clamp_to_band(best, low, high), "history"


# ----------------------------------------------------------------------------- production path


def category_history(env: Environment, company_id: int, category_id: int | None, exclude: int) -> list[tuple]:
    """(discount %, quantity, won) of the decided lines of the category (all products when none)."""
    extra = [("product_id.category_id", "=", category_id)] if category_id else []
    out: list[tuple[float, float, int]] = []
    for states, won in ((AC.WON, 1), (AC.LOST, 0)):
        for row in AC.history_lines(
            env, company_id, states, extra=extra, exclude_order=exclude, fields=["quantity", "discount"]
        ):
            out.append((float(row["discount"] or 0.0), float(row["quantity"] or 0.0), won))
    return out


def _cost_in_order(order: Model, line: Model) -> float:
    """The product's cost per unit of the line, in the order's currency (computation only, never shown)."""
    product = line.product_id.elevated()
    cost = float(product.standard_price or 0.0)
    if cost and line.uom_id and product.uom_id and line.uom_id.ids != product.uom_id.ids:
        cost = product.uom_id._compute_price(cost, line.uom_id)
    if cost and product.currency_id and product.currency_id.ids != order.currency_id.ids:
        cost = product.currency_id.convert(cost, order.currency_id, order.company_id, order.date_order.date())
    return float(cost)


def suggest(order: Model, *, propose: bool = True) -> dict[str, Any]:
    """Suggestions for every product line of ``order`` (as ``order.env``'s user), one proposal each."""
    from tessera.ai import proposals as P
    from tessera.ai.runtime import proposal_dict
    from tessera.ai.tools import PendingProposal

    env = order.env
    AC.ensure_enabled(env, FEATURE)
    order.ensure_one()
    order.check_access("read")
    started = time.monotonic()
    rec = order.elevated()
    editable = rec.state in ("draft", "sent") and not rec.locked
    bands = env["sale.discount_band"]
    models: dict[int | None, WinModel | None] = {}
    rows: list[dict[str, Any]] = []
    for line in AC.product_lines(rec):
        product = line.product_id
        qty = float(line.quantity or 0.0)
        band = bands._for_line(rec.company_id, product, rec.partner_id, qty)
        row: dict[str, Any] = {
            "line_id": line.ids[0],
            "product": product.display_name,
            "quantity": qty,
            "current_discount": float(line.discount or 0.0),
        }
        rows.append(row)
        if not band:
            row.update(status="no_band", reasons=[_("No approved discount band applies to this line")])
            continue
        b = band.elevated()
        low, high = float(b.min_discount or 0.0), float(b.max_discount or 0.0)
        cat = product.category_id.ids[0] if product.category_id else None
        if cat not in models:
            models[cat] = fit_win_model(category_history(env, rec.company_id.ids[0], cat, rec.ids[0]))
        model = models[cat]
        price = rec._price_for(product, qty, line.uom_id or None)
        value, method = choose_discount(low, high, qty, price, _cost_in_order(rec, line), model)
        if not within_band(value, low, high):  # cannot happen: the clamp above is the guarantee
            raise ValidationError(_("A discount suggestion left its band"))
        reasons = [_("Band {band}: {low} to {high} %", band=b.name, low=f"{low:g}", high=f"{high:g}")]
        if method == "history" and model is not None:
            reasons.append(
                _(
                    "Best expected margin given {won} won of {rows} decided quotations in this category",
                    won=model.won,
                    rows=model.rows,
                )
            )
        elif method == "quantity":
            reasons.append(_("Not enough won and lost history: placed in the band by quantity"))
        row.update(
            status="suggested",
            suggested_discount=value,
            band={"id": b.ids[0], "name": b.name, "min": low, "max": high},
            method=method,
            reasons=reasons,
        )
        if abs(value - row["current_discount"]) < STEP / 2:
            row["status"] = "unchanged"
    out: dict[str, Any] = {"lines": rows, "ai_generated": True}
    run = AC.audit_run(
        env,
        FEATURE,
        record=order,
        output=[
            {k: r.get(k) for k in ("line_id", "status", "suggested_discount", "method")}
            for r in rows
            if r["status"] != "no_band"
        ],
        model="banded win-rate model",
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    out["run_id"] = run.ids[0]
    if not propose or not editable:
        return out
    made = 0
    for row in rows:
        if row["status"] != "suggested":
            continue
        line = env["sale.quotation_line"].browse([row["line_id"]])
        try:
            line.check_access("write")
        except Exception:  # noqa: BLE001 - the user may read but not change the line: no proposal
            continue
        band = row["band"]
        if not within_band(row["suggested_discount"], band["min"], band["max"]):
            raise ValidationError(_("A discount suggestion left its band"))
        title = _(
            "Discount {discount} % on {product} (band {low}–{high} %)",
            discount=f"{row['suggested_discount']:g}",
            product=row["product"],
            low=f"{band['min']:g}",
            high=f"{band['max']:g}",
        )
        pending = PendingProposal(
            "write", "sale.quotation_line", row["line_id"], {"discount": row["suggested_discount"]}, title
        )
        row["proposal"] = proposal_dict(P.create(env, run, FEATURE, pending))
        made += 1
    if made:
        run.elevated().write({"decision": "pending"})
    return out


def acceptance(env: Environment, company_id: int | None = None) -> dict[str, Any]:
    """Outcomes of the discount proposals of a company: the acceptance rate counts accepted and edited
    proposals over decided ones (pending excluded)."""
    if not env.registry.security.has_groups(env, ["sale.group_sale_manager", "sale.group_sale_all"]):
        from tessera.exceptions import AccessError

        raise AccessError(_("Only sales managers see the acceptance of discount suggestions"))
    company_id = company_id or (env.company.ids[0] if env.company else None)
    proposals = env["ai_core.proposal"].elevated()
    counts = {s: 0 for s in ("pending", "accepted", "edited", "rejected", "expired", "failed")}
    for row in proposals.read_group([("feature", "=", FEATURE), ("company_id", "=", company_id)], [], ["state"]):
        counts[str(row["state"])] = int(row.get("__count") or 0)
    decided = counts["accepted"] + counts["edited"] + counts["rejected"] + counts["expired"]
    rate = (counts["accepted"] + counts["edited"]) / decided if decided else None
    return {"company_id": company_id, "counts": counts, "decided": decided, "acceptance_rate": rate}


def check_editable(order: Model) -> None:
    rec = order.elevated()
    if rec.state not in ("draft", "sent") or rec.locked:
        raise UserError(_("Only draft or sent quotations get discount suggestions"))


def explain_context(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    """Sources for the optional explanation of the suggestions (no proposal is made from it)."""
    from tessera.ai import Source

    if record is None or record._name != "sale.quotation":
        raise UserError(_("Discount suggestions work on a quotation"))
    found = suggest(record, propose=False)
    rec = record.elevated()
    text = "\n".join(
        f"- {r['product']} x {r['quantity']:g}: "
        + (
            f"suggested {r['suggested_discount']:g} % (now {r['current_discount']:g} %); " + "; ".join(r["reasons"])
            if r["status"] != "no_band"
            else "no approved band"
        )
        for r in found["lines"]
    )
    return {"sources": [Source(f"sale.quotation:{rec.ids[0]}", rec.name, f"Quotation {rec.name}\n{text}")]}


def _register() -> None:
    from tessera.ai.runtime import handler

    handler(FEATURE)(explain_context)


_register()
