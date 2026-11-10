"""Tax return report (2-e): golden grids on the EU template, tie to the ledger, US and generic templates,
tax closing entry and lock, tax return records, access rights and company isolation."""

from __future__ import annotations

import pytest

from modules.account.chart import chart_ref
from modules.account.tests.common import invoice, partner
from modules.account_reports.engine import run_report
from modules.account_tax.closing import ledger_check, period_of
from modules.account_tax.tests.common import EXPECTED_EU, Q2, D, eu_golden, line
from tessera.exceptions import AccessError, UserError, ValidationError

pytestmark = pytest.mark.db


def _values(env, company, start=Q2[0], end=Q2[1]):
    data = run_report(
        env, {"report": "tax", "date_from": start, "date_to": end, "company_ids": company.ids, "hide_zero": False}
    )
    return data, {line["code"]: line["values"][0] for line in data["lines"] if line["code"]}


@pytest.fixture
def golden(env):
    return eu_golden(env)


def test_eu_golden_grids(golden):
    docs = golden["docs"]
    assert docs["intra_eu"].fiscal_position_id and docs["export"].fiscal_position_id
    assert docs["reverse_charge"].amount_tax == pytest.approx(0)  # +100 % and -100 % cancel out
    data, values = _values(golden["env"], golden["company"])
    assert data["report"]["code"] == "tax_eu"
    assert {k: pytest.approx(v) for k, v in values.items() if k in EXPECTED_EU} == EXPECTED_EU
    # grid lines drill down to the tagged items
    g02 = next(line for line in data["lines"] if line["code"] == "G02")
    items = golden["env"]["account.move_line"].search(g02["domains"][0])
    assert -sum(float(i.balance) for i in items) == pytest.approx(210.0)


def test_the_report_ties_to_the_ledger(golden):
    check = ledger_check(golden["env"], golden["company"], *Q2)
    assert check["NET"] == pytest.approx(130.0)
    assert check["ledger_net"] == pytest.approx(130.0)
    assert check["difference"] == 0


def test_closing_entry_clears_tax_accounts_and_locks(golden):
    env, company = golden["env"], golden["company"]
    returns = env["account.tax_return"]
    ret = returns.browse([returns.action_close_period("2031-04-01", "2031-06-30")])
    assert ret.state == "closed" and ret.closed_by_id.ids == [env.uid]
    assert ret.amount_net == pytest.approx(130.0) and ret.amount_due == pytest.approx(410.0)
    assert ret.report_snapshot["report"]["code"] == "tax_eu"
    move = ret.closing_move_id
    assert move.state == "posted" and move.date == D(2031, 6, 30)
    by_account: dict[str, float] = {}
    for item in move.line_ids:
        by_account[item.account_id.code] = by_account.get(item.account_id.code, 0.0) + float(item.balance)
    assert by_account == {"2110": pytest.approx(410.0), "1410": pytest.approx(-280.0), "2100": pytest.approx(-130.0)}
    # per tax group: VAT 20 % owes 360 - 280 = 80, VAT 10 % owes 50
    payable = sorted(float(i.balance) for i in move.line_ids if i.account_id.code == "2100")
    assert payable == [pytest.approx(-80.0), pytest.approx(-50.0)]
    assert company.elevated().tax_lock_date == D(2031, 6, 30)
    # the tax accounts are cleared for the period; the report still ties (closing entries are excluded)
    tax_accounts = env["account.account"].search(
        [("code", "in", ["2110", "1410"]), ("company_id", "=", company.ids[0])]
    )
    period_items = env["account.move_line"].search(
        [("account_id", "in", tax_accounts.ids), ("date", ">=", "2031-04-01"), ("date", "<=", "2031-06-30")]
    )
    assert sum(float(i.balance) for i in period_items) == pytest.approx(0)
    assert ledger_check(env, company, *Q2)["difference"] == 0
    with pytest.raises(UserError):
        returns.action_close_period("2031-04-01", "2031-06-30")
    with pytest.raises(UserError):
        ret.action_refresh()
    with pytest.raises(UserError):
        ret.unlink()
    with pytest.raises(UserError):
        ret.write({"date_to": D(2031, 7, 31)})
    # taxed documents can no longer be posted in the closed period
    late = invoice(env, partner(env, "Late"), [line(env, 10.0, "sale_vat_20")], invoice_date=D(2031, 5, 2), post=False)
    with pytest.raises(UserError):
        late.action_post()


def test_closing_refuses_when_the_report_does_not_tie(golden):
    env = golden["env"]
    from modules.account_reports.tests.common import entry

    entry(env, D(2031, 5, 1), [("2110", -15.0), ("1000", 15.0)], company=golden["company"], ref="manual tax")
    assert ledger_check(env, golden["company"], *Q2)["difference"] == pytest.approx(-15.0)
    with pytest.raises(UserError):
        env["account.tax_return"].action_close_period("2031-04-01", "2031-06-30")
    ret_id = env["account.tax_return"].action_close_period("2031-04-01", "2031-06-30", force=True)
    assert env["account.tax_return"].browse([ret_id]).state == "closed"


def test_closing_needs_an_adviser_and_valid_dates(env, env_for):
    accountant = (
        env["core.user"]
        .elevated()
        .create(
            {
                "login": "tax_accountant",
                "name": "Tax accountant",
                "password": "secret-password-1",
                "group_ids": [("set", [env.ref("account.group_account_user").ids[0]])],
            }
        )
    )
    assert accountant
    with pytest.raises(AccessError):
        env_for("tax_accountant")["account.tax_return"].action_close_period("2031-01-01", "2031-03-31")
    with pytest.raises(UserError):
        env["account.tax_return"].action_close_period("2031-03-31", "2031-01-01")
    with pytest.raises(UserError):
        env["account.tax_return"].action_close_period("31/03/2031", "2031-01-01")


def test_us_template(env):
    customer = partner(env, "US Customer", country_id=env.ref("core.country_us").ids[0])
    invoice(env, customer, [line(env, 1000.0, "sale_combined_8_5")], invoice_date=D(2031, 4, 10))
    invoice(env, customer, [line(env, 100.0, "sale_combined_8_5")], move_type="out_refund", invoice_date=D(2031, 4, 11))
    data, values = _values(env, env.company)
    assert data["report"]["code"] == "tax_us"
    assert values["TX_SALES"] == pytest.approx(900.0)
    assert values["TX_STATE"] == pytest.approx(54.0)
    assert values["TX_LOCAL"] == pytest.approx(22.5)
    assert values["NET"] == pytest.approx(76.5)
    assert ledger_check(env, env.company, *Q2)["difference"] == 0


def test_generic_template(env):
    company = env["core.company"].create(
        {
            "name": "Sydney Pty",
            "country_id": env.ref("core.country_au").ids[0],
            "currency_id": env.ref("core.USD").ids[0],
        }
    )
    assert company.elevated().chart_template == "generic"
    env.user.elevated().write({"company_ids": [("add", company.ids[0])]})
    e = env.with_company(company)
    customer = partner(e, "Sydney customer")
    supplier = partner(e, "Sydney supplier")
    invoice(
        e,
        customer,
        [{"quantity": 1, "price_unit": 200.0, "tax_ids": [("set", chart_ref(company, "sale_tax_10").ids)]}],
        invoice_date=D(2031, 5, 1),
    )
    invoice(
        e,
        supplier,
        [{"quantity": 1, "price_unit": 50.0, "tax_ids": [("set", chart_ref(company, "purchase_tax_10").ids)]}],
        move_type="in_invoice",
        invoice_date=D(2031, 5, 1),
        date=D(2031, 5, 1),
    )
    data, values = _values(e, company)
    assert data["report"]["code"] == "tax_generic"
    assert (values["G01"], values["G02"], values["G04"], values["G05"]) == (200.0, 20.0, 50.0, 5.0)
    assert values["NET"] == pytest.approx(15.0)


def test_tax_return_records(golden):
    env, company = golden["env"], golden["company"]
    company.elevated().write({"account_tax_periodicity": "monthly"})
    assert period_of(company, D(2031, 5, 17)) == (D(2031, 5, 1), D(2031, 5, 31))
    company.elevated().write({"account_tax_periodicity": "quarterly"})
    assert period_of(company, D(2031, 5, 17)) == Q2
    returns = env["account.tax_return"]
    ret = returns.create({"date_from": Q2[0], "date_to": Q2[1]})
    assert ret.name == "Tax return 2031-04-01 - 2031-06-30" and ret.company_id == company
    ret.action_refresh()
    assert ret.amount_net == pytest.approx(130.0)
    assert ret.report_id.code == "tax_eu"
    assert ret.findings_summary
    following = returns.create({})
    assert (following.date_from, following.date_to) == (D(2031, 7, 1), D(2031, 9, 30))
    with pytest.raises(ValidationError):
        returns.create({"date_from": D(2031, 5, 1), "date_to": D(2031, 5, 31)})
    with pytest.raises(ValidationError):
        returns.create({"date_from": D(2032, 5, 1), "date_to": D(2032, 4, 30)})
    following.unlink()
    ret.action_close()
    assert ret.state == "closed"


def test_access_and_company_isolation(env, env_for, golden):
    e = golden["env"]
    ret = e["account.tax_return"].create({"date_from": Q2[0], "date_to": Q2[1]})
    users = env["core.user"].elevated()
    main = env.ref("core.main_company").ids[0]
    eu = golden["company"].ids[0]
    for login, group, companies in (
        ("tax_auditor", "account.group_account_readonly", [eu]),
        ("tax_us_accountant", "account.group_account_user", [main]),
    ):
        users.create(
            {
                "login": login,
                "name": login,
                "password": "secret-password-1",
                "group_ids": [("set", [env.ref(group).ids[0]])],
                "company_id": companies[0],
                "company_ids": [("set", companies)],
            }
        )
    auditor = env_for("tax_auditor")
    assert auditor["account.tax_return"].search([("id", "=", ret.ids[0])]).ids == ret.ids
    with pytest.raises(AccessError):
        auditor["account.tax_return"].create({"date_from": D(2032, 1, 1), "date_to": D(2032, 3, 31)})
    us = env_for("tax_us_accountant")
    assert not us["account.tax_return"].search([("id", "=", ret.ids[0])])
    created = us["account.tax_return"].create({"date_from": D(2032, 1, 1), "date_to": D(2032, 3, 31)})
    assert created.company_id.ids == [main]


def test_demo_step_creates_an_open_return(system_env):
    from modules.account_tax.demo import seed_tax_returns

    seed_tax_returns(system_env)
    seed_tax_returns(system_env)  # idempotent
    returns = system_env["account.tax_return"].search([("state", "=", "draft")])
    assert len(returns) >= 1
    assert len({r.company_id.ids[0] for r in returns}) == len(returns)
