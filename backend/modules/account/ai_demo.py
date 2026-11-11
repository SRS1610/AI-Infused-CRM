"""Demo step of the accounting AI features (``tessera seed demo --phase 2``, after the accounting demo): a bill
entered twice so the anomaly flags show on the bill form. Idempotent."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account.ai_anomaly import check_move
from modules.account.demo import DEMO_REF
from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)


def seed_company(env: Any, company: Any) -> int:
    env = env.with_company(company).with_context(skip_ai_anomaly=True)
    moves = env["account.move"]
    original = moves.search([("company_id", "=", company.ids[0]), ("ref", "=", f"{DEMO_REF}-BILL-03")], limit=1)
    if not original or moves.search_count(
        [("company_id", "=", company.ids[0]), ("ref", "=", f"{DEMO_REF}-BILL-03"), ("state", "=", "draft")]
    ):
        return 0
    twice = moves.create(
        {
            "move_type": "in_invoice",
            "partner_id": original.partner_id.ids[0],
            "invoice_date": (original.invoice_date or original.date) + dt.timedelta(days=2),
            "ref": original.ref,
            "invoice_line_ids": [
                (
                    "create",
                    {
                        "name": line.name,
                        "account_id": line.account_id.ids[0] if line.account_id else None,
                        "quantity": line.quantity,
                        "price_unit": line.price_unit,
                        "tax_ids": [("set", line.tax_ids.ids)],
                    },
                )
                for line in original.invoice_line_ids
            ],
        }
    )
    return len(check_move(env, twice, notify=False))


@hook("seed_demo")
def seed_ai_demo(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        log.info("accounting AI demo", company=company.name, flags=seed_company(env, company))
