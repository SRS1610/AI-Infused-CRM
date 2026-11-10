"""Analytic demo (``tessera seed demo --phase 2``): a *Departments* and a *Projects* plan, analytic accounts in
every company with a chart, a distribution model for rent, and posted invoices and bills spread over the
departments and projects. Idempotent."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
DEMO_REF = "DEMO-AN"
PLANS = {
    "departments": (
        "Departments",
        ["SAL:Sales", "MKT:Marketing", "RND:Research and development", "ADM:Administration"],
    ),
    "projects": ("Projects", ["WEB:Website redesign", "ROB:Robot line"]),
}


def _plan(env: Any, key: str, name: str) -> Any:
    xmlid = f"account_analytic.demo_plan_{key}"
    plan = env.ref(xmlid, raise_if_not_found=False)
    if plan is None:
        plan = env["account.analytic_plan"].create({"name": name, "sequence": 10 if key == "departments" else 20})
        env.set_ref(xmlid, plan, noupdate=True)
    return plan


def demo_accounts(env: Any, company: Any) -> dict[str, Any]:
    """The demo analytic accounts of ``company`` by code (created when missing)."""
    out: dict[str, Any] = {}
    model = env["account.analytic_account"].elevated()
    for key, (plan_name, accounts) in PLANS.items():
        plan = _plan(env.elevated(), key, plan_name)
        for spec in accounts:
            code, name = spec.split(":", 1)
            acc = model.search([("code", "=", code), ("company_id", "=", company.ids[0])], limit=1)
            if not acc:
                acc = model.create({"code": code, "name": name, "plan_id": plan.ids[0], "company_id": company.ids[0]})
            out[code] = acc
    return out


def seed_company(env: Any, company: Any) -> int:
    env = env.with_company(company)
    moves = env["account.move"]
    accounts = demo_accounts(env, company)
    if moves.search_count([("company_id", "=", company.ids[0]), ("ref", "like", f"{DEMO_REF}-")]):
        return 0
    rent = env["account.account"].search([("code", "=", "6100"), ("company_id", "=", company.ids[0])], limit=1)
    models = env["account.analytic_distribution_model"].elevated()
    if rent and not models.search([("account_prefix", "=", "6100"), ("company_id", "=", company.ids[0])]):
        models.create(
            {
                "account_prefix": "6100",
                "company_id": company.ids[0],
                "analytic_distribution": {str(accounts["ADM"].ids[0]): 50, str(accounts["SAL"].ids[0]): 50},
            }
        )
    partners = list(env["contacts.partner"].search([("is_company", "=", True)], limit=4))
    if not partners:
        return 0
    today = env.today()
    year_start = dt.date(today.year, 1, 1)
    codes = {
        c: env["account.account"].search([("code", "=", c), ("company_id", "=", company.ids[0])], limit=1)
        for c in ("6100", "6500", "6600", "4000")
    }
    plan = [
        # (month offset, move type, label, account code, amount, distribution)
        (0, "in_invoice", "Office rent", "6100", 2400.0, None),
        (0, "in_invoice", "Trade fair booth", "6600", 1800.0, {"MKT": 100, "ROB": 100}),
        (1, "in_invoice", "Design agency", "6500", 3200.0, {"MKT": 100, "WEB": 100}),
        (1, "in_invoice", "Prototype parts", "6500", 4100.0, {"RND": 100, "ROB": 100}),
        (2, "out_invoice", "Website project milestone", "4000", 6500.0, {"SAL": 100, "WEB": 100}),
        (2, "out_invoice", "Robot line consulting", "4000", 9200.0, {"SAL": 60, "RND": 40, "ROB": 100}),
        (3, "in_invoice", "Sales travel", "6600", 950.0, {"SAL": 100}),
        (3, "in_invoice", "Office rent", "6100", 2400.0, None),
    ]
    created = 0
    for i, (offset, move_type, label, code, amount, dist) in enumerate(plan):
        day = min(today, year_start + dt.timedelta(days=31 * offset + 10))
        account = codes.get(code)
        line: dict[str, Any] = {"name": label, "quantity": 1, "price_unit": amount, "tax_ids": [("set", [])]}
        if account:
            line["account_id"] = account.ids[0]
        if dist:
            line["analytic_distribution"] = {str(accounts[k].ids[0]): v for k, v in dist.items()}
        move = moves.create(
            {
                "move_type": move_type,
                "partner_id": partners[i % len(partners)].ids[0],
                "invoice_date": day,
                "ref": f"{DEMO_REF}-{i + 1:02d}",
                "invoice_line_ids": [("create", line)],
            }
        )
        move.action_post()
        created += 1
    return created


@hook("seed_demo")
def seed_analytic(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        count = seed_company(env, company)
        log.info("analytic demo", company=company.name, documents=count)
