"""Acceptance criterion 2-a: after the phase 2 demo seeding the trial balance sums to zero per company and in
the consolidated view, and the profit and loss reconciles with the balance sheet (net income → equity)."""

from __future__ import annotations

import pytest

from modules.account.demo import seed_company as seed_accounting
from modules.account.tests.common import eu_env
from modules.account_reports.demo import seed_company as seed_reports
from modules.account_reports.tests.common import by_code, row, run

pytestmark = pytest.mark.db


def _check_tb(data):
    opening, debit, credit, closing = row(data, "total")["values"]
    assert opening == pytest.approx(0, abs=0.005)
    assert closing == pytest.approx(0, abs=0.005)
    assert debit == pytest.approx(credit, abs=0.005)
    return debit


def test_2a_demo_ledgers_balance_and_statements_reconcile(env):
    env = eu_env(env)  # the admin works in both demo companies
    companies = env.ref("core.main_company") | env.ref("core.demo_company_eu")
    today = env.today()
    fy_start = today.replace(month=1, day=1)
    retained_before = {}
    for company in companies:
        seed_accounting(env, company)
        bs = by_code(run(env, "balance_sheet", company_ids=company.ids, date_to=today))
        retained_before[company.ids[0]] = bs["RETAINED_EARNINGS"]
        seed_reports(env, company)
    for company in companies:
        ids = company.ids
        movement = _check_tb(run(env, "trial_balance", company_ids=ids, date_from=fy_start, date_to=today))
        assert movement > 0  # the demo posted something this year
        bs = by_code(run(env, "balance_sheet", company_ids=ids, date_to=today))
        pl = by_code(run(env, "profit_loss", company_ids=ids, date_to=today))
        assert bs["ASSETS"] == pytest.approx(bs["LIABILITIES"] + bs["EQUITY"], abs=0.005)
        assert bs["CURRENT_EARNINGS"] == pytest.approx(pl["NET_INCOME"], abs=0.005)
        # last year's demo summary (72,000 revenue - 50,500 expenses) is in retained earnings with the capital
        added = bs["RETAINED_EARNINGS"] - retained_before[company.ids[0]]
        assert added == pytest.approx(50_000 + 21_500, abs=0.005)
        cf = by_code(run(env, "cash_flow", company_ids=ids, date_from=fy_start, date_to=today))
        assert cf["OPENING_CASH"] + cf["NET_CHANGE"] == pytest.approx(cf["CLOSING_CASH"], abs=0.005)
    consolidated = companies.ids
    _check_tb(run(env, "trial_balance", company_ids=consolidated, currency="USD", date_from=fy_start, date_to=today))
    bs = by_code(run(env, "balance_sheet", company_ids=consolidated, currency="USD", date_to=today))
    assert bs["ASSETS"] == pytest.approx(bs["LIABILITIES"] + bs["EQUITY"], abs=0.005)
