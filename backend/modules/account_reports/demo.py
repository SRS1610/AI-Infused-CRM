"""Reports demo data (``tessera seed demo --phase 2``): the share capital paid in and a summary of last year's
activity in every company with a chart, so the balance sheet shows retained earnings next to the current
year's (computed) earnings. Idempotent."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
DEMO_REF = "DEMO-REPORTS"


def seed_company(env: Any, company: Any) -> int:
    env = env.with_company(company)
    moves = env["account.move"]
    cid = company.ids[0]
    if moves.search_count([("company_id", "=", cid), ("ref", "like", f"{DEMO_REF}%")]):
        return 0
    accounts = env["account.account"]

    def acc(code: str) -> int | None:
        found = accounts.search([("code", "=", code), ("company_id", "=", cid)], limit=1)
        return found.ids[0] if found else None

    journal = env["account.journal"].search([("type", "=", "general"), ("company_id", "=", cid)], limit=1)
    bank, capital, services, rent, salaries = acc("1000"), acc("3000"), acc("4100"), acc("6100"), acc("6200")
    if not journal or None in (bank, capital, services, rent, salaries):
        return 0
    year = env.today().year - 1
    entries = [
        (dt.date(year, 1, 2), "capital", [(bank, 50_000.0), (capital, -50_000.0)]),
        (
            dt.date(year, 12, 31),
            "summary",
            [(bank, 21_500.0), (rent, 14_400.0), (salaries, 36_100.0), (services, -72_000.0)],
        ),
    ]
    created = 0
    for day, suffix, lines in entries:
        move = moves.create(
            {
                "move_type": "entry",
                "journal_id": journal.ids[0],
                "company_id": cid,
                "date": day,
                "ref": f"{DEMO_REF}-{suffix}",
                "line_ids": [
                    ("create", {"name": f"Demo {suffix}", "account_id": account, "balance": amount})
                    for account, amount in lines
                ],
            }
        )
        move.action_post()
        created += 1
    return created


@hook("seed_demo")
def seed_reports(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        count = seed_company(env, company)
        log.info("reports demo", company=company.name, entries=count)
