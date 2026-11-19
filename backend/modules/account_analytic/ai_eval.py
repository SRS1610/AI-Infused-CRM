# ruff: noqa: E501 - the rule table is kept one rule per line
"""Scenarios of the ``account_analytic.allocation_suggest`` evaluation (``ai/build_evals.py`` writes the cases).

:func:`build` creates, in the main demo company, two analytic plans (departments, projects), posted bills and
invoices of the last months whose lines carry distributions that follow a few house rules (a hosting vendor is
Operations, a travel agency is Sales except trade fairs that are Marketing, laptops are split Admin / Operations,
a client's invoices go to its project and to Sales, rent is Admin…), with about one line in eight allocated
differently (noise), optional distribution models, and finally the bill or invoice to allocate. The expected
distribution is the rule's (combined with the models' plans when a model applies).
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

D = dt.date
TODAY = D(2026, 10, 15)
# rule name → (partner, label templates, product, account code, distribution as {account key: percent})
RULES: dict[str, dict[str, Any]] = {
    "hosting": {"partner": "CloudNine Hosting", "labels": ["Server hosting {m}", "Cloud storage {m}", "Managed database {m}"], "account": "6500", "dist": {"ops": 100}, "kind": "in_invoice"},
    "travel": {"partner": "Wanderlust Travel", "labels": ["Flight to client meeting {m}", "Hotel sales visit {m}", "Train tickets customer workshop {m}"], "account": "6600", "dist": {"sales": 100}, "kind": "in_invoice"},
    "fair": {"partner": "Wanderlust Travel", "labels": ["Trade fair booth travel {m}", "Trade fair hotel {m}", "Expo trade fair flights {m}"], "account": "6600", "dist": {"marketing": 100}, "kind": "in_invoice"},
    "laptops": {"partner": "Byte Supplies", "labels": ["Laptop for new hire {m}", "Laptop replacement {m}", "Laptop docking kit {m}"], "product": "Laptop 14in", "account": "6000", "dist": {"admin": 50, "ops": 50}, "kind": "in_invoice"},
    "rent": {"partner": "Harbor Properties", "labels": ["Office rent {m}", "Office rent and charges {m}"], "account": "6100", "dist": {"admin": 100}, "kind": "in_invoice"},
    "alpha": {"partner": "Alpha Robotics", "labels": ["Integration services {m}", "Robot cell commissioning {m}", "Project Alpha milestone {m}"], "account": "4100", "dist": {"alpha": 100, "sales": 100}, "kind": "out_invoice"},
    "beta": {"partner": "Beta Logistics", "labels": ["Warehouse automation study {m}", "Beta rollout support {m}"], "account": "4100", "dist": {"beta": 100, "sales": 100}, "kind": "out_invoice"},
    "ads": {"partner": "Pixel Ads Agency", "labels": ["Online ads campaign {m}", "Social media ads {m}", "Display campaign {m}"], "account": "6000", "dist": {"marketing": 80, "sales": 20}, "kind": "in_invoice"},
}  # fmt: skip
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September"]


def _plans(e: Any, company: Any) -> dict[str, Any]:
    dep = e["account.analytic_plan"].create({"name": "Departments (eval)", "company_id": company.ids[0]})
    prj = e["account.analytic_plan"].create({"name": "Projects (eval)", "company_id": company.ids[0]})
    accounts = {}
    for key, plan, name in (
        ("sales", dep, "Sales"), ("ops", dep, "Operations"), ("admin", dep, "Administration"),
        ("marketing", dep, "Marketing"), ("alpha", prj, "Project Alpha"), ("beta", prj, "Project Beta"),
        ("gamma", prj, "Project Gamma"),
    ):  # fmt: skip
        accounts[key] = e["account.analytic_account"].create(
            {"name": name, "code": f"EV-{key}", "plan_id": plan.ids[0], "company_id": company.ids[0]}
        )
    return accounts


def _dist(accounts: dict[str, Any], spec: dict[str, float]) -> dict[str, float]:
    return {str(accounts[k].ids[0]): float(v) for k, v in spec.items()}


def build(env: Any, scenario: dict[str, Any]) -> tuple[Any, dict[str, float], dict[str, dict[str, float]]]:
    """(the document to allocate, the expected distribution, the expected distribution per plan)."""
    e = env.elevated("ai eval scenario")
    company = e.company
    rng = random.Random(scenario["seed"])  # noqa: S311 - reproducible synthetic data
    accounts = _plans(e, company)
    gl = {
        code: e["account.account"].search([("code", "=", code), ("company_id", "=", company.ids[0])], limit=1)
        for code in {r["account"] for r in RULES.values()}
    }
    partners: dict[str, Any] = {}
    products: dict[str, Any] = {}

    def partner(name: str) -> Any:
        if name not in partners:
            partners[name] = e["contacts.partner"].create(
                {"name": name + scenario.get("suffix", ""), "is_company": True}
            )
        return partners[name]

    def product(name: str) -> Any:
        if name not in products:
            products[name] = e["product.product"].create({"name": name})
        return products[name]

    def line(rule: dict[str, Any], label: str, dist: dict[str, float] | None) -> tuple[Any, ...]:
        vals: dict[str, Any] = {"name": label, "quantity": 1, "price_unit": round(rng.uniform(200, 4000), 2),
                                "account_id": gl[rule["account"]].ids[0], "tax_ids": [("set", [])]}  # fmt: skip
        if rule.get("product"):
            vals["product_id"] = product(rule["product"]).ids[0]
            vals["name"] = label
        if dist is not None:
            vals["analytic_distribution"] = dist
        return ("create", vals)

    history_rules = [r for r in RULES if r not in scenario.get("cold", [])]
    noise = scenario.get("noise", 0.12)
    for i, month in enumerate(MONTHS[: scenario.get("months", 6)]):
        day = TODAY.replace(month=1) + dt.timedelta(days=30 * i + 3)
        by_doc: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        for name in history_rules:
            rule = RULES[name]
            if rng.random() < 0.25:  # not every rule every month
                continue
            label = rng.choice(rule["labels"]).format(m=month)
            dist = rule["dist"]
            if rng.random() < noise:
                dist = {rng.choice(["sales", "ops", "admin", "marketing"]): 100}
            by_doc.setdefault((rule["partner"], rule["kind"]), []).append(line(rule, label, _dist(accounts, dist)))
        for (pname, kind), lines in by_doc.items():
            move = e["account.move"].create(
                {"move_type": kind, "partner_id": partner(pname).ids[0], "invoice_date": day, "date": day,
                 "invoice_line_ids": lines}
            )  # fmt: skip
            move.action_post()
    models_spec = scenario.get("model")
    model_dist: dict[str, float] = {}
    if models_spec:  # a distribution model on an account prefix fills the departments plan
        model_dist = _dist(accounts, models_spec["dist"])
        e["account.analytic_distribution_model"].create(
            {"account_prefix": models_spec["prefix"], "company_id": company.ids[0], "analytic_distribution": model_dist}
        )
    rule = RULES[scenario["rule"]]
    label = scenario.get("label") or rng.choice(rule["labels"]).format(m="October")
    query = e["account.move"].create(
        {"move_type": rule["kind"], "partner_id": partner(rule["partner"]).ids[0], "invoice_date": TODAY,
         "date": TODAY, "invoice_line_ids": [line(rule, label, None)]}
    )  # fmt: skip
    if scenario.get("post"):
        query.action_post()
    expected_spec = scenario.get("expected", rule["dist"])
    expected = _dist(accounts, expected_spec) if expected_spec else {}
    if model_dist:  # the model's plan wins; history fills the other plans
        dep_keys = {"sales", "ops", "admin", "marketing"}
        expected = {**{k: v for k, v in expected.items() if k not in {str(accounts[x].ids[0]) for x in dep_keys}},
                    **model_dist}  # fmt: skip
    by_plan: dict[str, dict[str, float]] = {}
    for key, acc in accounts.items():
        if str(acc.ids[0]) in expected:
            by_plan.setdefault("projects" if key in ("alpha", "beta", "gamma") else "departments", {})[
                str(acc.ids[0])
            ] = expected[str(acc.ids[0])]
    return query, expected, by_plan


def entry(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    from modules.account_analytic import ai_allocation as A

    query, expected, by_plan = build(env, inputs["scenario"])
    out = A.eval_entry(env, inputs, query.with_env(env))
    out["data"]["expected"] = expected
    out["data"]["by_plan"] = by_plan
    return out


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    from modules.account_analytic import ai_allocation as A

    for r in results:  # the expected distribution is only known once the scenario's records exist
        if isinstance(r.get("data"), dict) and "expected" in r["data"]:
            r["_case"]["expected"] = {
                **r["_case"]["expected"],
                "distribution": r["data"]["expected"],
                "by_plan": r["data"]["by_plan"],
            }
    return A.score(results)
