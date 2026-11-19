"""Dunning demo scenario: three levels (7, 30 and 60 days; a fee and a letter at 60 days), two late invoices
per company and a prepared reminder run to review and send."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)

LEVELS = [
    {
        "name": "First reminder",
        "delay_days": 7,
        "subject": "Friendly reminder: invoices from {company}",
        "message": "Hello {partner},\n\nThis is a friendly reminder that {count} invoice(s) totalling {amount_due} "
        "are past their due date. You can pay online with the links below.\n\nThank you,\n{company}",
    },
    {
        "name": "Second reminder",
        "delay_days": 30,
        "subject": "Second reminder: {amount_due} overdue",
        "message": "Hello {partner},\n\nDespite our first reminder, {amount_due} is still unpaid "
        "(up to {max_days} days late). Please settle it within 8 days or contact us.\n\n{company}",
    },
    {
        "name": "Final notice",
        "delay_days": 60,
        "print_letter": True,
        "fee_amount": 15.0,
        "subject": "Final notice before collection: {amount_due}",
        "message": "Hello {partner},\n\nThe invoices below are more than {max_days} days overdue. Without payment "
        "within 8 days we will hand the matter to our collection agency. A reminder fee is charged.\n\n{company}",
    },
]


def install_levels(company: Any) -> Any:
    env = company.env
    levels = env["account.dunning_level"].elevated()
    existing = levels.search([("company_id", "=", company.ids[0])])
    if existing:
        return existing
    fee_product = env["product.product"].elevated().search([("name", "=", "Late payment fee")], limit=1)
    if not fee_product:
        fee_product = (
            env["product.product"]
            .elevated()
            .create({"name": "Late payment fee", "type": "service", "list_price": 15.0})
        )
    return levels.create(
        [
            {
                **vals,
                "company_id": company.ids[0],
                "fee_product_id": fee_product.ids[0] if vals.get("fee_amount") else None,
            }
            for vals in LEVELS
        ]
    )


def seed_company(env: Any, company: Any) -> Any:
    from modules.account_dunning.dunning import prepare_run

    env = env.with_company(company)
    install_levels(company)
    moves = env["account.move"].elevated()
    if moves.search_count([("company_id", "=", company.ids[0]), ("ref", "like", "DEMO-LATE-")]):
        return None
    customer = (
        env["contacts.partner"]
        .elevated()
        .search([("is_company", "=", True), ("email", "!=", None)], order="id", limit=1)
    )
    if not customer:
        return None
    today = env.today()
    for i, ago in enumerate((40, 75), start=1):
        move = moves.create(
            {
                "move_type": "out_invoice",
                "partner_id": customer.ids[0],
                "company_id": company.ids[0],
                "invoice_date": today - dt.timedelta(days=ago),
                "invoice_date_due": today - dt.timedelta(days=ago),
                "payment_term_id": None,
                "ref": f"DEMO-LATE-{i}",
                "invoice_line_ids": [
                    ("create", {"name": "Consulting (late payment demo)", "quantity": 1, "price_unit": 300.0 * i})
                ],
            }
        )
        move.action_post()
    return prepare_run(company)


@hook("seed_demo")
def seed_dunning(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        run = seed_company(env, company)
        if run is not None:
            log.info("dunning demo", company=company.name, reminders=len(run.line_ids))
