"""Budgets: actuals from analytic items and journal items, theoretical amounts, monthly split, variance report."""

from __future__ import annotations

from typing import Any

import pytest

from modules.account.tests.common import D, eu_env, invoice, partner
from modules.account_analytic.tests.common import account, analytic, dist, plan
from tessera.exceptions import AccessError, UserError, ValidationError

pytestmark = pytest.mark.db


def _user(env: Any, login: str, groups: list[str], company: Any | None = None) -> Any:
    company = company or env.company
    return env["core.user"].create(
        {
            "name": login,
            "login": login,
            "company_id": company.ids[0],
            "company_ids": [("set", company.ids)],
            "group_ids": [("set", [env.ref(g).ids[0] for g in groups])],
        }
    )


@pytest.fixture
def ctx(env):
    dept = plan(env, "Departments")
    return {"mkt": analytic(env, dept, "MKT", "Marketing"), "sal": analytic(env, dept, "SAL", "Sales")}


def _budget(env, lines, **vals):
    return env["account.budget"].create(
        {
            "name": "Budget 2026",
            "date_from": D(2026, 1, 1),
            "date_to": D(2026, 12, 31),
            "line_ids": [("create", line) for line in lines],
            **vals,
        }
    )


def _bill(env, amount, distribution=None, code="6500", day=D(2026, 3, 10)):
    line: dict[str, Any] = {"price_unit": amount, "account_id": account(env, code).ids[0]}
    if distribution:
        line["analytic_distribution"] = distribution
    return invoice(env, partner(env, "Vendor"), [line], move_type="in_invoice", invoice_date=day, date=day)


def test_actuals_theoretical_and_variance(env, ctx):
    budget = _budget(
        env,
        [
            {"name": "Marketing", "analytic_account_id": ctx["mkt"].ids[0], "planned_amount": 1200.0},
            {
                "name": "Sales",
                "line_type": "revenue",
                "analytic_account_id": ctx["sal"].ids[0],
                "planned_amount": 5000.0,
            },
            {"name": "Software", "account_ids": [("set", account(env, "6500").ids)], "planned_amount": 1000.0},
            {"name": "All income", "line_type": "revenue", "account_type": "income", "planned_amount": 10000.0},
        ],
    )
    # other tests may have committed journal items: ledger-based lines are checked as differences
    base = {line.name: float(line.practical_amount) for line in budget.line_ids}
    _bill(env, 300.0, dist((ctx["mkt"], 100)))
    _bill(env, 200.0, dist((ctx["mkt"], 50)))
    _bill(env, 999.0, dist((ctx["mkt"], 100)), day=D(2027, 1, 5))  # outside the period
    invoice(env, partner(env), [{"price_unit": 4000.0, "analytic_distribution": dist((ctx["sal"], 100))}])
    lines = {line.name: line for line in budget.line_ids}
    mkt = lines["Marketing"]
    assert float(mkt.practical_amount) == 400.0
    assert float(mkt.variance) == -800.0 and float(mkt.achievement) == pytest.approx(33.33)
    assert float(lines["Sales"].practical_amount) == 4000.0 and float(lines["Sales"].variance) == -1000.0
    assert float(lines["Software"].practical_amount) - base["Software"] == 500.0
    assert float(lines["All income"].practical_amount) - base["All income"] == 4000.0
    assert float(budget.planned_total) == 5000.0 + 10000.0 - 1200.0 - 1000.0
    budget.with_context(budget_date="2026-06-30").action_refresh()
    assert float(mkt.theoretical_amount) == pytest.approx(1200.0 * 181 / 365, abs=0.01)
    budget.with_context(budget_date="2027-02-01").action_refresh()
    assert float(mkt.theoretical_amount) == 1200.0
    budget.with_context(budget_date="2025-12-01").action_refresh()
    assert float(mkt.theoretical_amount) == 0.0


def test_actuals_follow_posting_and_corrections(env, ctx):
    budget = _budget(
        env,
        [
            {"name": "Marketing", "analytic_account_id": ctx["mkt"].ids[0], "planned_amount": 1000.0},
            {"name": "Rent", "account_ids": [("set", account(env, "6100").ids)], "planned_amount": 1000.0},
        ],
    )
    mkt, rent = budget.line_ids.sorted(lambda r: r.ids[0])
    base = float(rent.practical_amount)  # other tests may have committed rent items
    bill = _bill(env, 100.0, dist((ctx["mkt"], 100)))
    assert float(mkt.practical_amount) == 100.0
    bill.invoice_line_ids.write({"analytic_distribution": dist((ctx["sal"], 100))})
    assert float(mkt.practical_amount) == 0.0
    misc = env["account.journal"].search([("type", "=", "general"), ("company_id", "=", env.company.ids[0])], limit=1)
    entry = env["account.move"].create(
        {
            "journal_id": misc.ids[0],
            "date": D(2026, 5, 31),
            "line_ids": [
                ("create", {"name": "Rent accrual", "account_id": account(env, "6100").ids[0], "balance": 750.0}),
                ("create", {"name": "Rent accrual", "account_id": account(env, "6000").ids[0], "balance": -750.0}),
            ],
        }
    )
    assert float(rent.practical_amount) == base  # drafts do not count
    entry.action_post()
    assert float(rent.practical_amount) == base + 750.0
    entry.button_draft()
    assert float(rent.practical_amount) == base
    # cancelled budgets are no longer refreshed
    budget.action_cancel()
    entry.action_post()
    assert float(rent.practical_amount) == base


def test_split_monthly(env, ctx):
    budget = _budget(
        env,
        [
            {
                "name": "Marketing",
                "analytic_account_id": ctx["mkt"].ids[0],
                "planned_amount": 1000.0,
                "date_from": D(2026, 1, 16),
                "date_to": D(2026, 4, 15),
            }
        ],
    )
    new_ids = budget.action_split_monthly()
    lines = env["account.budget_line"].browse(new_ids).sorted(lambda r: r.date_from)
    assert [(line.date_from, line.date_to) for line in lines] == [
        (D(2026, 1, 16), D(2026, 1, 31)),
        (D(2026, 2, 1), D(2026, 2, 28)),
        (D(2026, 3, 1), D(2026, 3, 31)),
        (D(2026, 4, 1), D(2026, 4, 15)),
    ]
    # 90 days: 16 / 28 / 31 / 15, cumulative rounding keeps the total exact
    assert [float(line.planned_amount) for line in lines] == [177.78, 311.11, 344.44, 166.67]
    assert sum(float(line.planned_amount) for line in lines) == pytest.approx(1000.0)
    assert lines[0].name == "Marketing 2026-01"
    assert len(budget.line_ids) == 4
    assert lines[0].action_split_monthly() == []  # single month: nothing to split


def test_states_and_constraints(env, ctx):
    with pytest.raises(UserError):
        env["account.budget"].create(
            {"name": "Empty", "date_from": D(2026, 1, 1), "date_to": D(2026, 12, 31)}
        ).action_confirm()
    with pytest.raises(ValidationError):
        _budget(env, [{"name": "Nothing", "planned_amount": 1.0}])
    with pytest.raises(ValidationError):
        _budget(
            env, [{"analytic_account_id": ctx["mkt"].ids[0], "date_from": D(2025, 12, 1), "date_to": D(2026, 1, 31)}]
        )
    with pytest.raises(ValidationError):
        env["account.budget"].create({"name": "Backwards", "date_from": D(2026, 12, 1), "date_to": D(2026, 1, 1)})
    budget = _budget(env, [{"analytic_account_id": ctx["mkt"].ids[0], "planned_amount": 100.0}])
    assert budget.line_ids.display_name == "Budget 2026 - [MKT] Marketing"
    budget.action_confirm()
    assert budget.state == "confirmed"
    with pytest.raises(UserError):
        budget.unlink()
    budget.action_done()
    with pytest.raises(UserError):
        budget.line_ids.write({"planned_amount": 5.0})
    with pytest.raises(UserError):
        budget.action_split_monthly()
    with pytest.raises(UserError):
        budget.action_cancel()
    other = _budget(env, [{"analytic_account_id": ctx["mkt"].ids[0], "planned_amount": 1.0}])
    other.action_cancel()
    other.action_draft()
    other.unlink()
    e = eu_env(env)
    with pytest.raises(ValidationError):
        _budget(e, [{"account_ids": [("set", account(env, "6100").ids)]}], company_id=e.company.ids[0])
    with pytest.raises(ValidationError):
        _budget(e, [{"analytic_account_id": analytic(env, plan(env, "X"), "X1").ids[0]}], company_id=e.company.ids[0])


def test_variance_report(env, ctx):
    budget = _budget(
        env,
        [
            {"name": "Marketing", "analytic_account_id": ctx["mkt"].ids[0], "planned_amount": 1000.0, "sequence": 1},
            {
                "name": "Sales",
                "line_type": "revenue",
                "analytic_account_id": ctx["sal"].ids[0],
                "planned_amount": 2000.0,
                "sequence": 2,
            },
        ],
    )
    _bill(env, 1250.0, dist((ctx["mkt"], 100)))
    invoice(env, partner(env), [{"price_unit": 2500.0, "analytic_distribution": dist((ctx["sal"], 100))}])
    from modules.account_budget.services import variance_report

    report = variance_report(budget, as_of="2026-07-02")
    assert report["as_of"] == "2026-07-02" and report["currency"] == env.company.currency_id.name
    mkt, sal = report["lines"]
    assert mkt == {
        **mkt,
        "name": "Marketing",
        "planned": 1000.0,
        "actual": 1250.0,
        "variance": 250.0,
        "variance_pct": 25.0,
        "achievement_pct": 125.0,
        "favourable": False,
        "theoretical": 501.37,
        "elapsed_share": 0.5014,
    }
    assert mkt["analytic_account"]["code"] == "MKT"
    assert sal["favourable"] is True and sal["variance"] == 500.0
    assert report["totals"]["expense"]["actual"] == 1250.0 and report["totals"]["revenue"]["planned"] == 2000.0
    assert report["net"] == {"planned": 1000.0, "actual": 1250.0, "variance": 250.0}


def test_refresh_job(env, system_env, ctx):
    budget = _budget(env, [{"analytic_account_id": ctx["mkt"].ids[0], "planned_amount": 365.0}])
    from modules.account_budget.services import refresh_budgets_job

    assert refresh_budgets_job(system_env.with_context(budget_date="2026-01-10"))["budgets"] >= 1
    assert float(env["account.budget_line"].browse(budget.line_ids.ids).theoretical_amount) == 10.0
    assert env.ref("account_budget.sched_budget_refresh").job_name == "account_budget.refresh"


def test_access_and_isolation(env, ctx):
    budget = _budget(env, [{"analytic_account_id": ctx["mkt"].ids[0], "planned_amount": 10.0}])
    auditor = env.with_user(_user(env, "auditor-bu@test", ["account.group_account_readonly"]).ids[0])
    assert auditor["account.budget"].browse(budget.ids).name == "Budget 2026"
    with pytest.raises(AccessError):
        auditor["account.budget"].create({"name": "No", "date_from": D(2026, 1, 1), "date_to": D(2026, 1, 2)})
    clerk = env.with_user(_user(env, "billing-bu@test", ["account.group_account_invoice"]).ids[0])
    with pytest.raises(AccessError):
        clerk["account.budget"].search([])
    # a billing user's bill still refreshes budgets it cannot read
    _bill(clerk, 40.0, dist((ctx["mkt"], 100)))
    assert float(budget.line_ids.practical_amount) == 40.0
    e = eu_env(env)
    eu_budget = e["account.budget"].create(
        {
            "name": "EU",
            "date_from": D(2026, 1, 1),
            "date_to": D(2026, 12, 31),
            "company_id": e.company.ids[0],
            "line_ids": [("create", {"account_type": "expense", "planned_amount": 5.0})],
        }
    )
    us_user = env.with_user(
        _user(env, "us-bu@test", ["account.group_account_user"], env.ref("core.main_company")).ids[0]
    )
    assert not us_user["account.budget"].search([("id", "=", eu_budget.ids[0])])
    assert not us_user["account.budget_line"].search([("budget_id", "=", eu_budget.ids[0])])


def test_demo(env):
    from modules.account_analytic.demo import seed_company as seed_analytic
    from modules.account_budget.demo import seed_company

    assert seed_company(env, env.company) == 1
    assert seed_company(env, env.company) == 0
    seed_analytic(env, env.company)
    budget = env["account.budget"].search([("company_id", "=", env.company.ids[0])], limit=1)
    assert budget.state == "confirmed"
    marketing = budget.line_ids.filtered(lambda line: (line.name or "").startswith("Marketing"))
    assert len(marketing) == 12 and sum(float(line.planned_amount) for line in marketing) == pytest.approx(12000.0)
