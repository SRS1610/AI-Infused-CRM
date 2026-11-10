"""``tessera perf reports`` seeding: copies of posted entries stay balanced and reports still reconcile."""

from __future__ import annotations

import pytest

from modules.account_reports.tests.common import D, by_code, entry, row, run
from tessera.cli.perf import _posted_items, clone_journal_items

pytestmark = pytest.mark.db


def test_clone_journal_items_keeps_the_books_balanced(env):
    company = env["core.company"].create(
        {"name": "Perf Co", "country_id": env.ref("core.country_us").ids[0], "currency_id": env.ref("core.USD").ids[0]}
    )
    env.user.elevated().write({"company_ids": [("add", company.ids[0])]})
    e = env.with_company(company)
    entry(e, D(2031, 6, 1), [("1000", 100.0), ("3000", -100.0)], company=company)
    entry(e, D(2031, 6, 2), [("1000", 40.0), ("4000", -40.0)], company=company)
    cid = company.ids[0]
    assert _posted_items(env.cr, cid) == 4
    added = clone_journal_items(env.cr, cid, 30)
    assert added >= 26
    assert clone_journal_items(env.cr, cid, 30) == 0  # already there
    unbalanced = env.cr.scalar(
        "SELECT count(*) FROM (SELECT move_id FROM account_move_line WHERE company_id = :c "
        "GROUP BY move_id HAVING round(sum(balance), 2) <> 0) s",
        {"c": cid},
    )
    assert unbalanced == 0
    tb = run(e, "trial_balance", company_ids=[cid], date_from=D(2029, 1, 1), date_to=D(2031, 12, 31))
    assert row(tb, "total")["values"][3] == pytest.approx(0, abs=0.005)
    bs = by_code(run(e, "balance_sheet", company_ids=[cid], date_to=D(2031, 12, 31)))
    assert bs["ASSETS"] == pytest.approx(bs["LIABILITIES_EQUITY"])
