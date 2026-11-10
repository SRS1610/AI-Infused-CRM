"""Accounting demo scenario (``tessera seed demo --phase 2``): invoices, bills, payments and a credit note in
every company with a chart, spread over the last months so aged balances, overdue filters and reports show
realistic data. Idempotent: skipped when the company already has demo invoices."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
DEMO_REF = "DEMO"


def _ref(env: Any, xmlid: str) -> Any:
    return env.ref(xmlid, raise_if_not_found=False)


def _customers(env: Any) -> list[Any]:
    out = [p for x in ("partner_acme", "partner_globex", "partner_initech") if (p := _ref(env, f"contacts.{x}"))]
    return out or list(env["contacts.partner"].search([("is_company", "=", True)], limit=3))


def seed_company(env: Any, company: Any) -> int:
    env = env.with_company(company)
    moves = env["account.move"]
    if moves.search_count([("company_id", "=", company.ids[0]), ("ref", "like", f"{DEMO_REF}-")]):
        return 0
    today = env.today()
    products = [
        p
        for x in ("product_consulting", "product_robot_arm", "product_support", "product_sensor")
        if (p := _ref(env, f"product.{x}"))
    ]
    sale_tax = company.default_sale_tax_id
    purchase_tax = company.default_purchase_tax_id
    for product in products:
        if sale_tax and not product._taxes_for(company, "sale"):  # keep taxes given by other modules (sale)
            product.write({"taxes_id": [("add", sale_tax.ids[0])]})
    customers = _customers(env)
    if not customers or not products:
        return 0
    term30 = _ref(env, "account.term_30")
    epd = _ref(env, "account.term_2_10_net_30")
    created = 0
    plan = [
        # (days ago, customer index, [(product index, qty)], payment: "full" | "partial" | None)
        (150, 0, [(0, 10), (2, 1)], "full"),
        (120, 1, [(1, 2)], "full"),
        (95, 2, [(0, 5)], "partial"),
        (75, 0, [(3, 20), (2, 1)], None),
        (60, 1, [(0, 8)], "full"),
        (45, 2, [(1, 1), (3, 4)], None),
        (30, 0, [(0, 12)], "partial"),
        (20, 1, [(2, 1)], None),
        (12, 2, [(0, 3)], "full"),
        (5, 0, [(1, 1)], None),
    ]
    invoices = []
    for i, (ago, cust, lines, pay) in enumerate(plan):
        day = today - dt.timedelta(days=ago)
        move = moves.create(
            {
                "move_type": "out_invoice",
                "partner_id": customers[cust % len(customers)].ids[0],
                "invoice_date": day,
                "ref": f"{DEMO_REF}-{i + 1:02d}",
                "payment_term_id": (epd if i % 4 == 0 else term30).ids[0] if term30 else None,
                "invoice_line_ids": [
                    (
                        "create",
                        {
                            "product_id": products[p % len(products)].ids[0],
                            "name": products[p % len(products)].name,
                            "quantity": qty,
                            "price_unit": float(products[p % len(products)].list_price or 100.0),
                            "tax_ids": [("set", sale_tax.ids if sale_tax else [])],
                        },
                    )
                    for p, qty in lines
                ],
            }
        )
        move.action_post()
        invoices.append((move, pay, day))
        created += 1
    for move, pay, day in invoices:
        when = min(today, day + dt.timedelta(days=20)).isoformat()
        if pay == "full":
            move.action_register_payment(date=when)
        elif pay == "partial":
            move.action_register_payment(amount=round(move.amount_total / 2, 2), date=when)
    # a credit note on the oldest unpaid invoice
    open_ = [m for m, pay, _d in invoices if pay is None]
    if open_:
        refund_ids = open_[0].action_reverse(reason="Damaged goods", mode="refund")
        refund = moves.browse(refund_ids)
        refund.write({"ref": f"{DEMO_REF}-CN"})
        refund.action_post()
        created += 1
    vendors = list(env["contacts.partner"].search([("is_company", "=", True)], limit=6))[3:] or customers
    expense_accounts = [
        a
        for code in ("6100", "6500", "6600")
        if (a := env["account.account"].search([("code", "=", code), ("company_id", "=", company.ids[0])], limit=1))
    ]
    for i, ago in enumerate((100, 70, 40, 15)):
        day = today - dt.timedelta(days=ago)
        account = expense_accounts[i % len(expense_accounts)] if expense_accounts else None
        bill = moves.create(
            {
                "move_type": "in_invoice",
                "partner_id": vendors[i % len(vendors)].ids[0],
                "invoice_date": day,
                "ref": f"{DEMO_REF}-BILL-{i + 1:02d}",
                "payment_term_id": term30.ids[0] if term30 else None,
                "invoice_line_ids": [
                    (
                        "create",
                        {
                            "name": ("Office rent", "Software subscriptions", "Travel")[i % 3],
                            "account_id": account.ids[0] if account else None,
                            "quantity": 1,
                            "price_unit": (2400.0, 480.0, 1250.0, 2400.0)[i],
                            "tax_ids": [("set", purchase_tax.ids if purchase_tax else [])],
                        },
                    )
                ],
            }
        )
        bill.action_post()
        if i < 2:
            bill.action_register_payment(date=min(today, day + dt.timedelta(days=25)).isoformat())
        created += 1
    return created


@hook("seed_demo")
def seed_accounting(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        count = seed_company(env, company)
        log.info("accounting demo", company=company.name, documents=count)
