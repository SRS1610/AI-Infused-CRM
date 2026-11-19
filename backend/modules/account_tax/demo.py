"""Tax return demo (``tessera seed demo --phase 2``): an open return for the current tax period of every
company with a chart, with its figures and pre-filing checks. Nothing is closed, so other demo steps can
still post in the period. Idempotent."""

from __future__ import annotations

from typing import Any

from modules.account_tax.closing import period_of
from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)


@hook("seed_demo")
def seed_tax_returns(env: Any) -> None:
    returns = env["account.tax_return"]
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        start, end = period_of(company, env.today())
        if returns.search_count([("company_id", "=", company.ids[0]), ("date_to", ">=", start)]):
            continue
        ret = returns.with_company(company).create({"company_id": company.ids[0], "date_from": start, "date_to": end})
        ret.action_refresh()
        log.info("tax return demo", company=company.name, period=f"{start}..{end}", net=ret.amount_net)
