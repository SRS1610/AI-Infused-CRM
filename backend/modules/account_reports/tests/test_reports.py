"""Report engine: trial balance, balance sheet and P&L reconciliation, comparison, unfolding, drill-down,
fiscal years, ledgers, aged balances, cash flow and filters. Dates are in 2030-2031 so the shared test
database's other data stays out of the way."""

from __future__ import annotations

import pytest

from modules.account.tests.common import invoice, partner
from modules.account_reports.tests.common import D, acc, by_code, domain_balance, entry, row, run

pytestmark = pytest.mark.db


@pytest.fixture
def books(env):
    """A small set of books in the main company: capital, prior-year activity, current-year invoices."""
    customer = partner(env, "Report Customer")
    vendor = partner(env, "Report Vendor")
    entry(env, D(2030, 1, 2), [("1000", 10_000.0), ("3000", -10_000.0)], ref="capital")
    entry(env, D(2030, 6, 30), [("1000", 3_000.0), ("4100", -5_000.0), ("6100", 2_000.0)], ref="2030 activity")
    inv = invoice(
        env,
        customer,
        [{"quantity": 2, "price_unit": 500.0, "account_id": acc(env, "4000").ids[0]}],
        invoice_date=D(2031, 2, 10),
    )
    bill = invoice(
        env,
        vendor,
        [{"quantity": 1, "price_unit": 300.0, "account_id": acc(env, "6500").ids[0]}],
        move_type="in_invoice",
        invoice_date=D(2031, 3, 5),
        date=D(2031, 3, 5),
    )
    entry(env, D(2031, 3, 20), [("1000", 400.0), ("1100", -400.0, customer)], ref="partial receipt")
    return {"customer": customer, "vendor": vendor, "invoice": inv, "bill": bill}


def _tb_totals(data):
    total = row(data, "total")["values"]
    return total


def test_trial_balance_sums_to_zero(env, books):
    data = run(env, "trial_balance", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31))
    opening, debit, credit, closing = _tb_totals(data)
    assert opening == pytest.approx(0, abs=0.005)
    assert closing == pytest.approx(0, abs=0.005)
    assert debit == pytest.approx(credit)
    assert debit == pytest.approx(1000 + 300 + 400)
    # 2030's result is not an opening balance of the P&L accounts but undistributed earnings
    undistributed = row(data, "undistributed")["values"]
    assert undistributed[0] == pytest.approx(-3_000.0)
    assert "acc:4100" not in {line["id"] for line in data["lines"]}  # zero in the period: hidden
    bank = row(data, "acc:1000")["values"]
    assert bank == [pytest.approx(13_000.0), pytest.approx(400.0), 0.0, pytest.approx(13_400.0)]
    # each row's domains find exactly its figures
    assert domain_balance(env, bank_domain := row(data, "acc:1000")["domains"][3]) == pytest.approx(13_400.0)
    assert bank_domain


def test_balance_sheet_balances_and_net_income_goes_to_equity(env, books):
    bs = run(env, "balance_sheet", date_to=D(2031, 3, 31))
    pl = run(env, "profit_loss", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31))
    v = by_code(bs)
    assert v["ASSETS"] == pytest.approx(v["LIABILITIES"] + v["EQUITY"])
    assert v["ASSETS"] == pytest.approx(v["LIABILITIES_EQUITY"])
    net = by_code(pl)["NET_INCOME"]
    assert net == pytest.approx(1000 - 300)
    assert v["CURRENT_EARNINGS"] == pytest.approx(net)
    # retained earnings = equity accounts + earlier years' results
    assert v["RETAINED_EARNINGS"] == pytest.approx(10_000 + 3_000)
    assert "CTA" not in v
    assert v["RECEIVABLES"] == pytest.approx(1000 - 400)
    assert v["PAYABLES"] == pytest.approx(300)


def test_profit_and_loss_lines(env, books):
    pl = by_code(run(env, "profit_loss", date_from=D(2030, 1, 1), date_to=D(2030, 12, 31)))
    assert pl["REVENUE"] == pytest.approx(5_000)
    assert pl["OPERATING_EXPENSES"] == pytest.approx(2_000)
    assert pl["GROSS_PROFIT"] == pytest.approx(5_000)
    assert pl["NET_INCOME"] == pytest.approx(3_000)


def test_profit_and_loss_defaults_to_the_fiscal_year(env, books):
    data = run(env, "profit_loss", date_to=D(2031, 3, 31))
    assert data["periods"][0] == {"date_from": "2031-01-01", "date_to": "2031-03-31"}
    assert by_code(data)["NET_INCOME"] == pytest.approx(700)


def test_fiscal_year_not_on_calendar(env, books):
    env["account.fiscal_year"].create({"name": "FY 2030/31", "date_from": D(2030, 7, 1), "date_to": D(2031, 6, 30)})
    entry(env, D(2030, 9, 1), [("1000", 100.0), ("4900", -100.0)], ref="sep 2030")
    bs = by_code(run(env, "balance_sheet", date_to=D(2031, 3, 31)))
    # the fiscal year started on 2030-07-01: September's income is current, June's is retained
    assert bs["CURRENT_EARNINGS"] == pytest.approx(700 + 100)
    assert bs["RETAINED_EARNINGS"] == pytest.approx(10_000 + 3_000)
    assert bs["ASSETS"] == pytest.approx(bs["LIABILITIES_EQUITY"])
    pl = run(env, "profit_loss", date_to=D(2031, 3, 31))
    assert pl["periods"][0]["date_from"] == "2030-07-01"
    tb = run(env, "trial_balance", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31))
    assert row(tb, "acc:4900")["values"][0] == pytest.approx(-100)  # P&L opening since 2030-07-01
    assert row(tb, "undistributed")["values"][0] == pytest.approx(-3_000)
    assert _tb_totals(tb)[0] == pytest.approx(0, abs=0.005)


def test_comparison_columns(env, books):
    data = run(
        env, "profit_loss", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), comparison="previous_year", periods=1
    )
    assert [c["date_from"] for c in data["columns"]] == ["2031-01-01", "2030-01-01"]
    data = run(
        env, "profit_loss", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), comparison="previous_period", periods=2
    )
    assert [(c["date_from"], c["date_to"]) for c in data["columns"]] == [
        ("2031-01-01", "2031-03-31"),
        ("2030-10-01", "2030-12-31"),
        ("2030-07-01", "2030-09-30"),
    ]
    net = by_code(data, 0)["NET_INCOME"], by_code(data, 2)["NET_INCOME"]
    assert net == (pytest.approx(700), pytest.approx(0))
    bs = run(env, "balance_sheet", date_to=D(2031, 3, 31), comparison="previous_period", periods=1)
    assert bs["columns"][1]["date_to"] == "2031-02-28"
    days = run(env, "profit_loss", date_from=D(2031, 3, 5), date_to=D(2031, 3, 14), comparison="previous_period")
    assert days["columns"][1]["date_from"] == "2031-02-23"
    tb = run(env, "trial_balance", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), comparison="previous_year")
    assert len(tb["columns"]) == 8
    gl = run(env, "general_ledger", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), comparison="previous_year")
    assert gl["warnings"]


def test_unfold_by_account_and_drill_down(env, books):
    data = run(env, "balance_sheet", date_to=D(2031, 3, 31))
    bank_line = row(data, f"line:{env.ref('account_reports.bs_bank').ids[0]}")
    assert bank_line["unfoldable"] and not bank_line["unfolded"]
    data = run(env, "balance_sheet", date_to=D(2031, 3, 31), unfold=[bank_line["id"]])
    child = row(data, f"{bank_line['id']}:acc:1000")
    assert child["values"][0] == pytest.approx(13_400)
    assert domain_balance(env, child["domains"][0]) == pytest.approx(13_400)
    assert domain_balance(env, bank_line["domains"][0]) == pytest.approx(13_400)
    retained = row(data, f"line:{env.ref('account_reports.bs_retained_earnings').ids[0]}")
    assert -domain_balance(env, retained["domains"][0]) == pytest.approx(13_000)
    everything = run(env, "balance_sheet", date_to=D(2031, 3, 31), unfold_all=True)
    assert any(line["style"] == "account" for line in everything["lines"])


def test_filters_journals_partners_and_drafts(env, books):
    sales = env["account.journal"].search([("type", "=", "sale"), ("company_id", "=", env.company.ids[0])], limit=1)
    pl = by_code(run(env, "profit_loss", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), journal_ids=sales.ids))
    assert pl["REVENUE"] == pytest.approx(1000)
    assert pl["OPERATING_EXPENSES"] == pytest.approx(0)
    pl = by_code(
        run(env, "profit_loss", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), partner_ids=books["vendor"].ids)
    )
    assert pl["NET_INCOME"] == pytest.approx(-300)
    entry(env, D(2031, 3, 30), [("1000", 50.0), ("4900", -50.0)], post=False, ref="draft")
    posted = by_code(run(env, "profit_loss", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31)))
    drafts = by_code(run(env, "profit_loss", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), include_draft=True))
    assert drafts["NET_INCOME"] - posted["NET_INCOME"] == pytest.approx(50)


def test_general_ledger_running_balance_and_pagination(env, books):
    for day in range(1, 6):
        entry(env, D(2031, 1, day), [("1010", 10.0), ("3000", -10.0)], ref=f"cash {day}")
    data = run(env, "general_ledger", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), unfold=["acc:1010"], limit=2)
    cash = row(data, "acc:1010")
    assert cash["values"][4:] == [pytest.approx(50), 0.0, pytest.approx(50)]
    items = [line for line in data["lines"] if line["parent_id"] == "acc:1010"]
    assert [i["style"] for i in items] == ["initial", "item", "item", "load_more"]
    assert [i["values"][6] for i in items[1:3]] == [pytest.approx(10), pytest.approx(20)]
    assert items[1]["move_id"]
    more = items[-1]
    assert more["next_offset"] == 2 and more["remaining"] == 3
    page = run(
        env, "general_ledger", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), expand="acc:1010", offset=2, limit=2
    )
    assert [line["values"][6] for line in page["lines"] if line["style"] == "item"] == [
        pytest.approx(30),
        pytest.approx(40),
    ]
    assert page["lines"][-1]["next_offset"] == 4
    total = row(data, "total")["values"]
    assert total[4] == pytest.approx(total[5])  # every entry balances
    receivable = run(env, "general_ledger", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), unfold_all=True)
    items = [line for line in receivable["lines"] if line["parent_id"] == "acc:1100" and line["style"] == "item"]
    assert {i["values"][2] for i in items} == {"Report Customer"}


def test_partner_ledger(env, books):
    data = run(env, "partner_ledger", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), unfold_all=True)
    customer = row(data, f"partner:{books['customer'].ids[0]}")
    assert customer["name"] == "Report Customer"
    assert customer["values"][4:] == [pytest.approx(1000), pytest.approx(400), pytest.approx(600)]
    vendor = row(data, f"partner:{books['vendor'].ids[0]}")
    assert vendor["values"][6] == pytest.approx(-300)
    assert domain_balance(env, customer["domains"][6]) == pytest.approx(600)
    only_payable = run(env, "partner_ledger", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), account_type="payable")
    assert [line["id"] for line in only_payable["lines"]] == [f"partner:{books['vendor'].ids[0]}", "total"]


def test_aged_receivable_uses_residuals_as_of_the_date(env):
    customer = partner(env, "Aged Customer")
    old = invoice(env, customer, [{"quantity": 1, "price_unit": 100.0}], invoice_date=D(2031, 1, 10))
    recent = invoice(env, customer, [{"quantity": 1, "price_unit": 40.0}], invoice_date=D(2031, 3, 25))
    old.action_register_payment(date="2031-04-15")
    columns = ["due", "not_due", "d30", "d60", "d90", "d120", "older", "total"]
    data = run(env, "aged_receivable", date_to=D(2031, 3, 31), unfold_all=True)
    assert [c["key"] for c in data["columns"]] == columns
    line = row(data, f"partner:{customer.ids[0]}")
    # paid on 15 April: still open on 31 March, 80 days past due
    assert line["values"][1:] == [0.0, pytest.approx(40), 0.0, pytest.approx(100), 0.0, 0.0, pytest.approx(140)]
    items = [r for r in data["lines"] if r["parent_id"] == line["id"]]
    assert {i["move_id"] for i in items} == {old.ids[0], recent.ids[0]}
    later = run(env, "aged_receivable", date_to=D(2031, 4, 30))
    line = row(later, f"partner:{customer.ids[0]}")
    assert line["values"][-1] == pytest.approx(40)
    assert line["values"][3] == pytest.approx(40)  # 36 days past due


def test_aged_payable(env):
    vendor = partner(env, "Aged Vendor")
    invoice(
        env,
        vendor,
        [{"quantity": 1, "price_unit": 250.0}],
        move_type="in_invoice",
        invoice_date=D(2031, 1, 1),
        date=D(2031, 1, 1),
    )
    data = run(env, "aged_payable", date_to=D(2031, 6, 30), limit=1, unfold=[f"partner:{vendor.ids[0]}"])
    line = row(data, f"partner:{vendor.ids[0]}")
    assert line["values"][6] == pytest.approx(250)  # older than 120 days, shown positive
    assert line["unfolded"]


def test_cash_flow_reconciles_to_the_change_in_cash(env, books):
    entry(env, D(2031, 3, 1), [("1500", 800.0), ("1000", -800.0)], ref="machine")
    entry(env, D(2031, 3, 2), [("1000", 2_000.0), ("2500", -2_000.0)], ref="loan")
    entry(env, D(2031, 3, 3), [("1010", 100.0), ("1000", -100.0)], ref="transfer")
    data = run(env, "cash_flow", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31))
    v = by_code(data)
    assert v["OPENING_CASH"] == pytest.approx(13_000)
    assert v["CUSTOMERS"] == pytest.approx(400)
    assert v["INVESTING"] == pytest.approx(-800)
    assert v["FINANCING"] == pytest.approx(2_000)
    assert v["OPENING_CASH"] + v["NET_CHANGE"] == pytest.approx(v["CLOSING_CASH"])
    investing = row(data, f"line:{env.ref('account_reports.cf_investing').ids[0]}")
    assert -domain_balance(env, investing["domains"][0]) == pytest.approx(-800)


def test_hide_zero_and_report_errors(env, books):
    from tessera.exceptions import MissingError, UserError

    with pytest.raises(MissingError):
        run(env, "no_such_report")
    with pytest.raises(UserError):
        run(env, "profit_loss", date_from=D(2031, 3, 1), date_to=D(2031, 1, 1))
    with pytest.raises(MissingError):
        run(env, "balance_sheet", currency="XXX")
    shown = run(env, "trial_balance", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31), hide_zero=False)
    hidden = run(env, "trial_balance", date_from=D(2031, 1, 1), date_to=D(2031, 3, 31))
    assert len(shown["lines"]) > len(hidden["lines"])
