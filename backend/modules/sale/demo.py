"""Sales demo scenario (``tessera seed demo --phase 2``): sales orders in several invoicing states in the main
company: confirmed and partly invoiced, with a down payment, with promotions, fully invoiced (locked).
Idempotent: skipped when the demo orders exist."""

from __future__ import annotations

from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
DEMO_REF = "DEMO-SO"


def _ref(env: Any, xmlid: str) -> Any:
    return env.ref(xmlid, raise_if_not_found=False)


def _order(env: Any, partner: Any, ref: str, lines: list[tuple[Any, float]], **vals: Any) -> Any:
    return env["sale.quotation"].create(
        {
            "partner_id": partner.ids[0],
            "client_reference": ref,
            "line_ids": [("create", {"product_id": p.ids[0], "quantity": qty}) for p, qty in lines],
            **vals,
        }
    )


def seed_sales(env: Any) -> int:
    company = env.ref("core.main_company")
    env = env.with_company(company)
    orders = env["sale.quotation"]
    if orders.search_count([("client_reference", "like", f"{DEMO_REF}-")]):
        return 0
    acme = _ref(env, "contacts.partner_acme_jordan")
    initech = _ref(env, "contacts.partner_initech_priya")
    arm = _ref(env, "product.product_robot_arm")
    onboarding = _ref(env, "product.product_onboarding")
    consulting = _ref(env, "product.product_consulting")
    support = _ref(env, "product.product_support")
    if not all((acme, initech, arm, onboarding, consulting, support)):
        return 0
    created = 0
    # 1. confirmed, one robot arm and the onboarding delivered and invoiced, one arm still to deliver
    partly = _order(env, acme, f"{DEMO_REF}-01", [(arm, 2), (onboarding, 1)])
    partly.action_confirm()
    for line in partly.line_ids.filtered(lambda r: r.product_id.ids in (arm.ids, onboarding.ids) and not r.is_optional):
        line.write({"qty_delivered": 1})
    env["account.move"].browse(partly.action_create_invoice()["invoice_ids"]).action_post()
    created += 1
    # 2. consulting engagement with a 30 % down payment invoiced
    deposit = _order(env, initech, f"{DEMO_REF}-02", [(consulting, 10)])
    deposit.action_confirm()
    env["account.move"].browse(deposit.action_create_invoice("percentage", 30)["invoice_ids"]).action_post()
    created += 1
    # 3. robot cell from the template, three arms: the automatic promotions apply, plus a welcome code
    template = _ref(env, "sale.template_robot_cell")
    promo = _order(env, acme, f"{DEMO_REF}-03", [], template_id=template.ids[0] if template else None)
    arm_line = promo.line_ids.filtered(lambda r: r.product_id.ids == arm.ids and not r.is_optional)
    if arm_line:
        arm_line.write({"quantity": 3})
    else:
        promo.write({"line_ids": [("create", {"product_id": arm.ids[0], "quantity": 3})]})
    if _ref(env, "sale.promotion_welcome"):
        promo.action_apply_promotions(code="WELCOME100")
    else:
        promo.action_apply_promotions()
    promo.action_confirm()
    created += 1
    # 4. annual support, invoiced in full: the order is locked
    done = _order(env, acme, f"{DEMO_REF}-04", [(support, 1)])
    done.action_confirm()
    env["account.move"].browse(done.action_create_invoice()["invoice_ids"]).action_post()
    created += 1
    return created


@hook("seed_demo")
def seed_sale_orders(env: Any) -> None:
    count = seed_sales(env)
    log.info("sales demo", orders=count)
