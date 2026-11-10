"""Consolidation: companies converted into the report currency, translation differences, trial balance zero."""

from __future__ import annotations

import pytest

from modules.account_reports.engine import Rates
from modules.account_reports.tests.common import D, by_code, entry, isolated, row, run

pytestmark = pytest.mark.db


@pytest.fixture
def group(env):
    main_env = isolated(env, "Group US")
    e = isolated(main_env, "Group France", country="fr", currency="EUR")
    eu = e.company
    main = main_env.company
    eur = eu.elevated().currency_id
    rates = env["core.currency_rate"].elevated()
    for day, rate in ((D(2031, 1, 1), 0.90), (D(2031, 2, 1), 0.80), (D(2031, 3, 1), 0.75)):
        rates.create({"currency_id": eur.ids[0], "date": day, "rate": rate})
    entry(e, D(2031, 1, 5), [("1000", 1000.0), ("3000", -1000.0)], company=eu, ref="eu capital")
    entry(e, D(2031, 2, 15), [("1000", 500.0), ("4100", -500.0)], company=eu, ref="eu sale")
    entry(main_env, D(2031, 1, 10), [("1000", 2000.0), ("3000", -2000.0)], company=main, ref="us capital")
    entry(main_env, D(2031, 3, 10), [("1000", 300.0), ("4100", -300.0)], company=main, ref="us sale")
    return {"env": e, "eu": eu, "main": main, "ids": [main.ids[0], eu.ids[0]]}


AVERAGE_Q1 = (31 / 0.9 + 28 / 0.8 + 31 / 0.75) / 90  # USD per EUR, time-weighted over Q1 2031


def test_rates_average_is_time_weighted(group):
    env = group["env"]
    rates = Rates(env)
    usd = env.ref("core.USD").ids[0]
    eur = group["eu"].elevated().currency_id.ids[0]
    assert rates.factor(eur, usd, D(2031, 3, 31)) == pytest.approx(1 / 0.75)
    assert rates.average(eur, usd, D(2031, 1, 1), D(2031, 3, 31)) == pytest.approx(AVERAGE_Q1)
    assert rates.average(eur, eur, D(2031, 1, 1), D(2031, 3, 31)) == 1.0
    assert rates.average(eur, usd, None, D(2031, 3, 31)) == pytest.approx(1 / 0.75)


def test_consolidated_balance_sheet_balances_with_translation_adjustment(group):
    env = group["env"]
    data = run(env, "balance_sheet", date_to=D(2031, 3, 31), company_ids=group["ids"], currency="USD")
    assert data["consolidated"]
    v = by_code(data)
    assert v["BANK"] == pytest.approx(2300 + 1500 / 0.75, abs=0.01)
    assert v["CURRENT_EARNINGS"] == pytest.approx(300 + 500 * AVERAGE_Q1, abs=0.01)
    assert v["RETAINED_EARNINGS"] == pytest.approx(2000 + 1000 / 0.75, abs=0.01)
    assert v["CTA"] == pytest.approx(500 / 0.75 - 500 * AVERAGE_Q1, abs=0.02)
    assert v["ASSETS"] == pytest.approx(v["LIABILITIES"] + v["EQUITY"], abs=0.005)
    pl = by_code(run(env, "profit_loss", date_to=D(2031, 3, 31), company_ids=group["ids"], currency="USD"))
    assert pl["NET_INCOME"] == pytest.approx(v["CURRENT_EARNINGS"], abs=0.01)


def test_consolidated_trial_balance_sums_to_zero(group):
    env = group["env"]
    data = run(
        env, "trial_balance", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), company_ids=group["ids"], currency="USD"
    )
    opening, debit, credit, closing = row(data, "total")["values"]
    assert opening == pytest.approx(0, abs=0.005)
    assert closing == pytest.approx(0, abs=0.005)
    assert debit == pytest.approx(credit, abs=0.005)
    assert row(data, "cta")["values"] != [0.0, 0.0, 0.0, 0.0]
    # accounts are merged by code across companies
    bank = row(data, "acc:1000")
    assert bank["values"][3] == pytest.approx(2300 + 1500 / 0.75, abs=0.01)
    # same companies in their own currency: no translation line
    eur = run(env, "trial_balance", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), company_ids=[group["eu"].ids[0]])
    assert "cta" not in {line["id"] for line in eur["lines"]}
    assert row(eur, "acc:1000")["values"][3] == pytest.approx(1500)


def test_consolidated_cash_flow_and_ledgers(group):
    env = group["env"]
    cf = by_code(
        run(env, "cash_flow", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), company_ids=group["ids"], currency="USD")
    )
    assert cf["OPENING_CASH"] + cf["NET_CHANGE"] + cf["TRANSLATION"] == pytest.approx(cf["CLOSING_CASH"], abs=0.01)
    assert cf["TRANSLATION"] != 0
    gl = run(
        env,
        "general_ledger",
        date_from=D(2031, 1, 1),
        date_to=D(2031, 3, 31),
        company_ids=group["ids"],
        currency="USD",
        unfold_all=True,
    )
    bank = row(gl, "acc:1000")
    assert bank["values"][6] == pytest.approx(2300 + 1500 / 0.75, abs=0.01)  # closing rate of the report date
    items = [line for line in gl["lines"] if line["parent_id"] == "acc:1000" and line["style"] == "item"]
    assert items[-1]["values"][6] == pytest.approx(bank["values"][6], abs=0.01)
