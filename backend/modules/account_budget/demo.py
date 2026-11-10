"""Budget demo (``tessera seed demo --phase 2``): a confirmed budget for the current year in every company with
a chart, on the demo departments and projects plus a travel line on the financial account, with the
marketing line split per month. Idempotent."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account_analytic.demo import demo_accounts
from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)


def seed_company(env: Any, company: Any) -> int:
    env = env.with_company(company)
    today = env.today()
    name = f"Budget {today.year}"
    budgets = env["account.budget"]
    if budgets.search_count([("company_id", "=", company.ids[0]), ("name", "=", name)]):
        return 0
    accounts = demo_accounts(env, company)
    travel = env["account.account"].search([("code", "=", "6600"), ("company_id", "=", company.ids[0])], limit=1)
    lines: list[tuple[str, dict[str, Any]]] = [
        (
            "create",
            {
                "name": "Sales revenue",
                "line_type": "revenue",
                "analytic_account_id": accounts["SAL"].ids[0],
                "planned_amount": 60000.0,
            },
        ),
        ("create", {"name": "Marketing", "analytic_account_id": accounts["MKT"].ids[0], "planned_amount": 12000.0}),
        (
            "create",
            {
                "name": "Research and development",
                "analytic_account_id": accounts["RND"].ids[0],
                "planned_amount": 18000.0,
            },
        ),
        (
            "create",
            {"name": "Administration", "analytic_account_id": accounts["ADM"].ids[0], "planned_amount": 15000.0},
        ),
        (
            "create",
            {
                "name": "Robot line project",
                "analytic_account_id": accounts["ROB"].ids[0],
                "line_type": "revenue",
                "planned_amount": 20000.0,
            },
        ),
    ]
    if travel:
        lines.append(("create", {"name": "Travel", "account_ids": [("set", travel.ids)], "planned_amount": 6000.0}))
    budget = budgets.create(
        {
            "name": name,
            "date_from": dt.date(today.year, 1, 1),
            "date_to": dt.date(today.year, 12, 31),
            "company_id": company.ids[0],
            "line_ids": lines,
        }
    )
    budget.line_ids.filtered(lambda line: line.name == "Marketing").action_split_monthly()
    budget.action_confirm()
    return 1


@hook("seed_demo")
def seed_budgets(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        count = seed_company(env, company)
        log.info("budget demo", company=company.name, budgets=count)
