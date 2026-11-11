"""``sale.pricing_breach`` — pricing-policy flags on quotations (notification feature, no proposal).

Rules and statistics, computed in-process, per product line:

* ``below_margin`` — the net unit price (after the line's discount) is below the product's cost plus the
  band's minimum margin (no band: below cost);
* ``discount_above_band`` — the line's discount is above the maximum of its approved band;
* ``below_pricelist`` — a typed price is below the pricelist price by more than the band allows (the
  effective discount against the pricelist, discount included, is above the band's maximum);
* ``unusual_for_customer`` — the discount is far above what this customer usually gets on this product (else
  its category) in their confirmed orders: more than three standard deviations and ten points above their
  mean, from at least three earlier lines (a warning, not a breach).

Flags are stored on ``sale.pricing_flag`` (shown on the quotation) when a quotation is sent or confirmed and
whenever someone runs the check; sales administrators are notified of new breaches. Costs are read for the
computation only: no message contains a cost. An approver of the line's band can approve a flag; a changed line
is checked again.
"""

from __future__ import annotations

import math
import time
from typing import TYPE_CHECKING, Any

from modules.sale import ai_common as AC
from tessera.i18n import _
from tessera.logging import get_logger
from tessera.orm.hooks import hook

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)
FEATURE = "sale.pricing_breach"
EPS = 1e-6
UNUSUAL_MIN_LINES = 3
UNUSUAL_SIGMAS = 3.0
UNUSUAL_POINTS = 10.0
SIGMA_FLOOR = 2.0


def _customer_discounts(env: Environment, order: Model, product: Model) -> list[float]:
    """Discounts the customer got on this product (else its category) in their confirmed orders."""
    partner = order.commercial_partner_id or order.partner_id
    base = [("quotation_id.commercial_partner_id", "=", partner.ids[0])]
    rows = AC.history_lines(
        env,
        order.company_id.ids[0],
        AC.WON,
        extra=[*base, ("product_id", "=", product.ids[0])],
        exclude_order=order.ids[0],
        fields=["discount"],
    )
    if len(rows) < UNUSUAL_MIN_LINES and product.category_id:
        rows = AC.history_lines(
            env,
            order.company_id.ids[0],
            AC.WON,
            extra=[*base, ("product_id.category_id", "=", product.category_id.ids[0])],
            exclude_order=order.ids[0],
            fields=["discount"],
        )
    return [float(r["discount"] or 0.0) for r in rows]


def _cost(order: Model, line: Model) -> float:
    from modules.sale.ai_discount import _cost_in_order

    return _cost_in_order(order, line)


def findings(order: Model) -> list[dict[str, Any]]:
    """Every breach of ``order`` (computed with elevated reads; the caller checked access)."""
    rec = order.elevated("pricing check")
    env = rec.env
    bands = env["sale.discount_band"]
    out: list[dict[str, Any]] = []
    for line in AC.product_lines(rec):
        product = line.product_id
        qty = float(line.quantity or 0.0)
        band = bands._for_line(rec.company_id, product, rec.partner_id, qty)
        discount = float(line.discount or 0.0)
        net = line._price_reduced()
        list_price = AC.safe_price(rec, product, qty, line.uom_id or None) or 0.0

        def add(
            code: str,
            severity: str,
            message: str,
            value: float,
            line: Model = line,
            band: Model = band,
            net: float = net,
            qty: float = qty,
        ) -> None:
            out.append(
                {
                    "line_id": line.ids[0],
                    "product": line.product_id.display_name,
                    "code": code,
                    "severity": severity,
                    "message": message,
                    "band_id": band.ids[0] if band else None,
                    "value": round(value, 2),
                    "signature": f"{code}:{line.ids[0]}:{round(value, 2)}:{round(net, 2)}:{round(qty, 3)}",
                }
            )

        cost = _cost(rec, line)
        margin = float(band.min_margin or 0.0) if band else 0.0
        if cost > 0 and net < cost * (1 + margin / 100.0) - EPS:
            add(
                "below_margin",
                "breach",
                _("Net price below cost plus the {margin} % minimum margin", margin=f"{margin:g}")
                if margin
                else _("Net price below cost"),
                discount,
            )
        if band and discount > float(band.max_discount or 0.0) + EPS:
            add(
                "discount_above_band",
                "breach",
                _(
                    "Discount {discount} % above the {band} band (at most {max} %)",
                    discount=f"{discount:g}",
                    band=band.name,
                    max=f"{band.max_discount:g}",
                ),
                discount,
            )
        if band and line.price_manual and list_price > 0:
            effective = (1 - net / list_price) * 100
            if effective > float(band.max_discount or 0.0) + 0.01 and discount <= float(band.max_discount or 0.0) + EPS:
                add(
                    "below_pricelist",
                    "breach",
                    _(
                        "Typed price {percent} % below the pricelist, more than the {band} band allows ({max} %)",
                        percent=f"{effective:.1f}",
                        band=band.name,
                        max=f"{band.max_discount:g}",
                    ),
                    effective,
                )
        history = _customer_discounts(env, rec, product)
        if len(history) >= UNUSUAL_MIN_LINES:
            mean = sum(history) / len(history)
            sigma = max(SIGMA_FLOOR, math.sqrt(sum((x - mean) ** 2 for x in history) / len(history)))
            if discount > mean + max(UNUSUAL_SIGMAS * sigma, UNUSUAL_POINTS):
                add(
                    "unusual_for_customer",
                    "warning",
                    _(
                        "Discount {discount} % is unusual for this customer (usually {mean} % over {count} lines)",
                        discount=f"{discount:g}",
                        mean=f"{mean:.1f}",
                        count=len(history),
                    ),
                    discount,
                )
    return out


def check(order: Model) -> list[dict[str, Any]]:
    """Live findings of ``order`` for its user (read access checked, feature switched on)."""
    AC.ensure_enabled(order.env, FEATURE)
    order.ensure_one()
    order.check_access("read")
    return findings(order)


def flag_dict(flag: Model) -> dict[str, Any]:
    f = flag.elevated()
    return {
        "id": f.ids[0],
        "line_id": f.line_id.ids[0] if f.line_id else None,
        "product": f.line_id.product_id.display_name if f.line_id and f.line_id.product_id else None,
        "code": f.code,
        "severity": f.severity,
        "message": f.message,
        "state": f.state,
        "band": f.band_id.name if f.band_id else None,
        "approved_by": f.approved_by.name if f.approved_by else None,
        "ai_generated": True,
    }


def sync_flags(order: Model, *, notify: bool = True) -> dict[str, Any]:
    """Store the findings of ``order`` as flags (keeping decisions on unchanged lines), resolve the flags that
    no longer hold and notify the sales administrators of new breaches. Runs as ``order.env``'s user for the
    audit trail; the flags themselves are written by the system."""
    env = order.env
    started = time.monotonic()
    rec = order.elevated("pricing check")
    found = findings(order)
    flags = env["sale.pricing_flag"].elevated("pricing check")
    existing = flags.search([("quotation_id", "=", rec.ids[0]), ("state", "in", ["open", "approved"])])
    by_sig = {f.signature: f for f in existing}
    run = AC.audit_run(
        env,
        FEATURE,
        record=order,
        output=[{k: f[k] for k in ("line_id", "code", "severity", "value")} for f in found],
        model="pricing rules + customer history",
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    new: list[dict[str, Any]] = []
    keep: set[str] = set()
    for f in found:
        keep.add(f["signature"])
        if f["signature"] in by_sig:
            continue
        flags.create(
            {
                "quotation_id": rec.ids[0],
                "line_id": f["line_id"],
                "code": f["code"],
                "severity": f["severity"],
                "message": f["message"],
                "band_id": f["band_id"],
                "value": f["value"],
                "signature": f["signature"],
                "ai_run_id": run.ids[0],
            }
        )
        new.append(f)
    stale = existing.filtered(lambda x: x.signature not in keep)
    if stale:
        stale.write({"state": "resolved"})
    breaches = [f for f in new if f["severity"] == "breach"]
    if notify and breaches:
        managers = AC.sales_managers(env, rec.company_id.ids[0])
        if managers:
            env["core.notification"].elevated().notify(
                managers,
                _("Pricing to review on {order}", order=rec.name),
                "; ".join(f"{f['product']}: {f['message']}" for f in breaches[:5]),
                kind="system",
                model="sale.quotation",
                res_id=rec.ids[0],
                url=f"/m/sale.quotation/{rec.ids[0]}",
            )
    current = flags.search([("quotation_id", "=", rec.ids[0]), ("state", "in", ["open", "approved"])])
    return {"run_id": run.ids[0], "new": len(new), "flags": [flag_dict(f) for f in current], "ai_generated": True}


@hook("after_write")
def check_on_send_or_confirm(records: Model, vals: dict[str, Any], old_values: Any) -> None:
    """A quotation that is sent or confirmed gets its pricing checked (managers notified of new breaches).
    The check never blocks the sale: any error is logged."""
    if records._name != "sale.quotation" or vals.get("state") not in ("sent", "accepted"):
        return
    env = records.env
    if not AC.ai_installed(env) or env.context.get("sale_no_pricing_check"):
        return
    for order in records:
        company = order.elevated().company_id.ids[0]
        if not AC.feature_enabled(env, FEATURE, company):
            continue
        try:
            with env.cr.savepoint():
                sync_flags(order)
        except Exception as exc:
            log.warning("pricing check failed", order=order.ids[0], error=str(exc))


def explain_context(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    """Sources for the optional summary of the flags (rule messages only: no cost reaches the prompt)."""
    from tessera.ai import Source
    from tessera.exceptions import UserError

    if record is None or record._name != "sale.quotation":
        raise UserError(_("Pricing flags are checked on a quotation"))
    found = check(record)
    rec = record.elevated()
    text = "\n".join(f"- {f['product']}: {f['message']} ({f['severity']})" for f in found) or "No flag."
    return {"sources": [Source(f"sale.quotation:{rec.ids[0]}", rec.name, f"Quotation {rec.name}\n{text}")]}


def _register() -> None:
    from tessera.ai.runtime import handler

    handler(FEATURE)(explain_context)


_register()
