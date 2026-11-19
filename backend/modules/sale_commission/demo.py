"""Commission demo scenario (``tessera seed demo --phase 2``) and the 2-k cross-check data.

For a month, Hugo Lambert and Nadia Keller (the demo plan's salespeople) get invoices — two from sales orders — and
credit notes, plus documents that must not count: an invoice of the month before, a draft invoice and another
salesperson's invoice. ``tests/data/commission_crosscheck.csv`` holds the statements expected for that month,
computed separately (``tests/data/make_crosscheck.py``). The seeding step uses the last complete month and computes
its statements. Idempotent per month."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
REF = "DEMO-COM"

# document key → (salesperson, day of the month, how, lines: (product xmlid, quantity, unit price, discount %))
DOCUMENTS: list[tuple[str, str, int, str, list[tuple[str, float, float, float]]]] = [
    ("A1", "hugo", 5, "order", [("product_robot_arm", 2, 18900, 5), ("product_onboarding", 1, 2500, 0)]),
    ("A2", "hugo", 12, "invoice", [("product_consulting", 10, 1200, 0), ("product_gripper", 3, 1450, 12.5)]),
    ("B1", "nadia", 8, "invoice", [("product_support", 1, 4800, 0), ("product_sensor", 3, 3200, 7.5)]),
    ("B2", "nadia", 18, "order", [("product_robot_arm", 1, 18900, 0)]),
    ("B3", "nadia", 26, "refund", [("product_sensor", 1, 3200, 0)]),
    # not counted: a draft, and another salesperson's invoice
    ("X1", "hugo", 27, "draft", [("product_sensor", 1, 3200, 0)]),
    ("X2", "admin", 14, "invoice", [("product_consulting", 4, 1200, 0)]),
]
# A3: credit note of A2 on the 22nd for 2 of the 10 consulting days (made by reversing A2)


def _ref(env: Any, xmlid: str) -> Any:
    return env.ref(xmlid, raise_if_not_found=False)


def _users(env: Any) -> dict[str, Any]:
    return {
        "hugo": _ref(env, "sale_commission.user_hugo"),
        "nadia": _ref(env, "sale_commission.user_nadia"),
        "admin": _ref(env, "core.user_admin"),
    }


def seed_month(env: Any, month_start: dt.date) -> int:
    """Create the scenario's documents in the month starting ``month_start``. Returns how many were created."""
    company = env.ref("core.main_company")
    env = env.with_company(company)
    label = f"{month_start:%Y-%m}"
    moves = env["account.move"]
    if moves.search_count([("ref", "like", f"{REF}-{label}-")]):
        return 0
    users = _users(env)
    customer = _ref(env, "contacts.partner_acme") or env["contacts.partner"].search(
        [("is_company", "=", True)], limit=1
    )
    pricelist = _ref(env, "product.pricelist_public")
    currency = company.currency_id
    if not all(users.values()) or not customer:
        return 0
    products = {x: _ref(env, f"product.{x}") for doc in DOCUMENTS for x, *_r in doc[4]}
    if not all(products.values()):
        return 0

    def lines(spec: list[tuple[str, float, float, float]]) -> list[dict[str, Any]]:
        return [
            {"product_id": products[x].ids[0], "quantity": q, "price_unit": p, "discount": disc}
            for x, q, p, disc in spec
        ]

    created: dict[str, Any] = {}
    # the month before: does not count
    previous = month_start - dt.timedelta(days=3)
    docs = [("X0", "hugo", previous, "invoice", [("product_consulting", 1, 1200, 0)])]
    docs += [(k, who, month_start.replace(day=day), how, spec) for k, who, day, how, spec in DOCUMENTS]
    for key, who, day, how, spec in docs:
        user = users[who]
        if how == "order":
            order = env["sale.quotation"].create(
                {
                    "partner_id": customer.ids[0],
                    "user_id": user.ids[0],
                    "pricelist_id": pricelist.ids[0] if pricelist else None,
                    "currency_id": currency.ids[0],
                    "client_reference": f"{REF}-{label}-{key}",
                    "line_ids": [("create", vals) for vals in lines(spec)],
                }
            )
            order.action_confirm()
            for line in order.line_ids.filtered(lambda r: r.product_id and not r.is_optional):
                line.write({"qty_delivered": line.quantity})  # delivered in full
            move = moves.browse(order.action_create_invoice()["invoice_ids"])
            move.write({"invoice_date": day, "ref": f"{REF}-{label}-{key}"})
        else:
            move = moves.create(
                {
                    "move_type": "out_refund" if how == "refund" else "out_invoice",
                    "partner_id": customer.ids[0],
                    "currency_id": currency.ids[0],
                    "invoice_user_id": user.ids[0],
                    "invoice_date": day,
                    "ref": f"{REF}-{label}-{key}",
                    "invoice_line_ids": [("create", vals) for vals in lines(spec)],
                }
            )
        if how != "draft":
            move.action_post()
        created[key] = move
    # A3: 2 of A2's 10 consulting days credited on the 22nd, by reversing A2 (the credit note's salesperson is A2's)
    a2 = created["A2"]
    credit = moves.browse(
        a2.action_reverse(reason="2 days not delivered", date=month_start.replace(day=22).isoformat())
    )
    consulting = products["product_consulting"]
    commands: list[Any] = [
        ("update", item.ids[0], {"quantity": 2}) if item.product_id.ids == consulting.ids else ("delete", item.ids[0])
        for item in credit.invoice_line_ids
    ]
    credit.write({"invoice_line_ids": commands, "ref": f"{REF}-{label}-A3"})
    credit.action_post()
    return len(created) + 1


def seed_commissions(env: Any, month_start: dt.date) -> Any:
    """The scenario's documents and the demo plan's statements for that month."""
    seed_month(env, month_start)
    plan = _ref(env, "sale_commission.plan_demo")
    if plan is None:
        return env["sale.commission_statement"]
    from modules.sale_commission.engine import generate_statements

    return generate_statements(plan, month_start)


@hook("seed_demo")
def seed_commission_demo(env: Any) -> None:
    first = env.today().replace(day=1)
    last_month = (first - dt.timedelta(days=1)).replace(day=1)
    statements = seed_commissions(env, last_month)
    log.info("commission demo", statements=len(statements))
