"""Analytic plans, distributions, analytic items, distribution models and mandatory plans."""

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
def setup(env):
    dept = plan(env, "Departments")
    proj = plan(env, "Projects")
    return {
        "dept": dept,
        "proj": proj,
        "sales": analytic(env, dept, "SAL", "Sales"),
        "mkt": analytic(env, dept, "MKT", "Marketing"),
        "web": analytic(env, proj, "WEB", "Website"),
    }


def _lines(move):
    return move.mapped("line_ids.analytic_line_ids")


def test_posting_creates_analytic_items_with_income_positive(env, setup):
    s = setup
    customer = partner(env)
    move = invoice(
        env,
        customer,
        [{"price_unit": 1000.0, "analytic_distribution": dist((s["sales"], 60), (s["mkt"], 40), (s["web"], 100))}],
    )
    items = env["account.analytic_line"].search([("move_id", "=", move.ids[0])])
    by_account = {line.account_id.code: float(line.amount) for line in items}
    assert by_account == {"SAL": 600.0, "MKT": 400.0, "WEB": 1000.0}
    sample = items.filtered(lambda line: line.account_id.code == "SAL")
    assert sample.plan_id == s["dept"] and sample.general_account_id.code == "4000"
    assert sample.partner_id == customer and sample.date == D(2026, 3, 10) and float(sample.percentage) == 60.0
    assert float(s["web"].balance) == 1000.0 and float(s["web"].credit) == 1000.0

    bill = invoice(
        env,
        customer,
        [
            {
                "price_unit": 300.0,
                "account_id": account(env, "6500").ids[0],
                "analytic_distribution": dist((s["mkt"], 100)),
            }
        ],
        move_type="in_invoice",
    )
    cost = env["account.analytic_line"].search([("move_id", "=", bill.ids[0])])
    assert float(cost.amount) == -300.0
    mkt = env["account.analytic_account"].browse(s["mkt"].ids)
    assert float(mkt.balance) == 100.0 and float(mkt.debit) == 300.0


def _entry(env, amount, distribution, day=None):
    misc = env["account.journal"].search([("type", "=", "general"), ("company_id", "=", env.company.ids[0])], limit=1)
    return env["account.move"].create(
        {
            "journal_id": misc.ids[0],
            "date": day or D(2026, 4, 1),
            "line_ids": [
                (
                    "create",
                    {
                        "name": "Accrual",
                        "account_id": account(env, "6100").ids[0],
                        "balance": amount,
                        "analytic_distribution": distribution,
                    },
                ),
                ("create", {"name": "Accrual", "account_id": account(env, "6000").ids[0], "balance": -amount}),
            ],
        }
    )


def test_reset_and_cancel_remove_items(env, setup):
    move = _entry(env, 50.0, dist((setup["sales"], 100)))
    move.action_post()
    assert len(_lines(move)) == 1
    move.button_draft()
    assert not env["account.analytic_line"].search([("move_id", "=", move.ids[0])])
    move.action_post()
    assert len(env["account.analytic_line"].search([("move_id", "=", move.ids[0])])) == 1
    move.button_cancel()
    assert not env["account.analytic_line"].search([("move_id", "=", move.ids[0])])


def test_posted_distribution_can_be_corrected(env, setup):
    s = setup
    move = invoice(env, partner(env), [{"price_unit": 200.0, "analytic_distribution": dist((s["sales"], 100))}])
    line = move.invoice_line_ids
    line.write({"analytic_distribution": dist((s["mkt"], 50))})
    items = env["account.analytic_line"].search([("move_id", "=", move.ids[0])])
    assert [(i.account_id.code, float(i.amount)) for i in items] == [("MKT", 100.0)]
    # through the invoice form: an update command on a posted entry's line
    move.write({"invoice_line_ids": [("update", line.ids[0], {"analytic_distribution": dist((s["web"], 100))})]})
    items = env["account.analytic_line"].search([("move_id", "=", move.ids[0])])
    assert [(i.account_id.code, float(i.amount)) for i in items] == [("WEB", 200.0)]
    # other fields stay read-only
    with pytest.raises(UserError):
        move.write({"invoice_line_ids": [("update", line.ids[0], {"price_unit": 1.0})]})
    with pytest.raises(UserError):
        line.write({"price_unit": 1.0})
    line.write({"analytic_distribution": None})
    assert not env["account.analytic_line"].search([("move_id", "=", move.ids[0])])


def test_distribution_validation(env, setup):
    s = setup
    move = invoice(env, partner(env), [{"price_unit": 10.0}], post=False)
    line = move.invoice_line_ids
    with pytest.raises(ValidationError):
        line.write({"analytic_distribution": dist((s["sales"], 60), (s["mkt"], 50))})  # 110 % on one plan
    with pytest.raises(ValidationError):
        line.write({"analytic_distribution": {"999999": 10}})
    with pytest.raises(ValidationError):
        line.write({"analytic_distribution": {str(s["sales"].ids[0]): 0}})
    with pytest.raises(ValidationError):
        line.write({"analytic_distribution": {"abc": 10}})
    with pytest.raises(ValidationError):
        line.write({"analytic_distribution": [1, 2]})
    line.write({"analytic_distribution": {s["sales"].ids[0]: "60", str(s["web"].ids[0]): 100}})
    assert line.analytic_distribution == {str(s["sales"].ids[0]): 60.0, str(s["web"].ids[0]): 100.0}
    e = eu_env(env)
    foreign = analytic(e, s["dept"], "EU1", company_id=e.company.ids[0])
    with pytest.raises(ValidationError):
        line.write({"analytic_distribution": dist((foreign, 10))})
    with pytest.raises(ValidationError):
        env["account.analytic_account"].create(
            {
                "name": "x",
                "plan_id": plan(e, "EU only", company_id=e.company.ids[0]).ids[0],
                "company_id": env.company.ids[0],
            }
        )


def test_journal_entry_items_and_reversal_keep_distribution(env, setup):
    s = setup
    misc = env["account.journal"].search([("type", "=", "general"), ("company_id", "=", env.company.ids[0])], limit=1)
    entry = env["account.move"].create(
        {
            "journal_id": misc.ids[0],
            "date": D(2026, 4, 1),
            "line_ids": [
                (
                    "create",
                    {
                        "name": "Accrual",
                        "account_id": account(env, "6100").ids[0],
                        "balance": 500.0,
                        "analytic_distribution": dist((s["mkt"], 100)),
                    },
                ),
                ("create", {"name": "Accrual", "account_id": account(env, "6000").ids[0], "balance": -500.0}),
            ],
        }
    )
    entry.action_post()
    assert float(env["account.analytic_line"].search([("move_id", "=", entry.ids[0])]).amount) == -500.0
    reversal = env["account.move"].browse(entry.action_reverse(mode="cancel"))
    items = env["account.analytic_line"].search([("move_id", "=", reversal.ids[0])])
    assert [float(i.amount) for i in items] == [500.0]
    assert float(env["account.analytic_account"].browse(s["mkt"].ids).balance) == 0.0
    inv = invoice(env, partner(env), [{"price_unit": 80.0, "analytic_distribution": dist((s["web"], 100))}])
    refund = env["account.move"].browse(inv.action_reverse(mode="refund"))
    assert refund.invoice_line_ids.analytic_distribution == dist((s["web"], 100))


def test_distribution_models_prefill_new_lines(env, setup):
    s = setup
    models = env["account.analytic_distribution_model"]
    vip = partner(env, "VIP Customer")
    product = env["product.product"].create({"name": "Analytic widget", "list_price": 10.0})
    models.create({"account_prefix": "4", "analytic_distribution": dist((s["sales"], 100)), "sequence": 50})
    models.create({"partner_id": vip.ids[0], "analytic_distribution": dist((s["mkt"], 100), (s["web"], 100))})
    models.create(
        {"partner_id": vip.ids[0], "product_id": product.ids[0], "analytic_distribution": dist((s["sales"], 30))}
    )
    plain = invoice(env, partner(env, "Someone"), [{"price_unit": 10.0}], post=False)
    assert plain.invoice_line_ids.analytic_distribution == dist((s["sales"], 100))
    vip_move = invoice(env, vip, [{"price_unit": 10.0}], post=False)
    assert vip_move.invoice_line_ids.analytic_distribution == dist((s["mkt"], 100), (s["web"], 100))
    # the most specific model (partner + product) wins its plan; the partner model still fills the project plan
    both = invoice(env, vip, [{"price_unit": 10.0, "product_id": product.ids[0]}], post=False)
    assert both.invoice_line_ids.analytic_distribution == dist((s["sales"], 30), (s["web"], 100))
    # an explicit distribution (even empty) is kept
    explicit = invoice(env, vip, [{"price_unit": 10.0, "analytic_distribution": None}], post=False)
    assert not explicit.invoice_line_ids.analytic_distribution
    # lines added later get the models too
    plain.write({"invoice_line_ids": [("create", {"name": "Second", "price_unit": 5.0})]})
    assert all(line.analytic_distribution == dist((s["sales"], 100)) for line in plain.invoice_line_ids)
    from modules.account_analytic.services import distribution_for

    assert distribution_for(env, {"account_code": "6100"}) == {}
    assert distribution_for(env, {"account_id": account(env, "4000").ids[0]}) == dist((s["sales"], 100))


def test_distribution_model_by_category_and_company(env, setup):
    s = setup
    parent = env["product.category"].create({"name": "Hardware"})
    child = env["product.category"].create({"name": "Sensors", "parent_id": parent.ids[0]})
    product = env["product.product"].create({"name": "Sensor X", "category_id": child.ids[0]})
    e = eu_env(env)
    env["account.analytic_distribution_model"].create(
        {"product_categ_id": parent.ids[0], "analytic_distribution": dist((s["web"], 100))}
    )
    found = env["account.analytic_distribution_model"].distribution_for({"product_id": product.ids[0]})
    assert found == dist((s["web"], 100))
    eu_mkt = analytic(e, s["dept"], "EUM", company_id=e.company.ids[0])
    e["account.analytic_distribution_model"].create(
        {"company_id": e.company.ids[0], "analytic_distribution": dist((eu_mkt, 100))}
    )
    assert env["account.analytic_distribution_model"].distribution_for({"company_id": e.company.ids[0]}) == dist(
        (eu_mkt, 100)
    )
    with pytest.raises(ValidationError):
        e["account.analytic_distribution_model"].create(
            {"company_id": e.company.ids[0], "analytic_distribution": dist((s["mkt"], 100))}
        )
    with pytest.raises(ValidationError):
        env["account.analytic_distribution_model"].create({"analytic_distribution": dist((s["mkt"], 120))})


def test_mandatory_plan_refuses_posting(env, setup):
    s = setup
    s["dept"].write({"applicability_ids": [("create", {"account_type": "expense", "applicability": "mandatory"})]})
    vendor = partner(env, "Supplier")
    bill = invoice(
        env,
        vendor,
        [{"price_unit": 100.0, "account_id": account(env, "6500").ids[0]}],
        move_type="in_invoice",
        post=False,
    )
    with pytest.raises(UserError):
        bill.action_post()
    bill.invoice_line_ids.write({"analytic_distribution": dist((s["sales"], 50))})
    with pytest.raises(UserError):
        bill.action_post()  # 50 % is not enough
    bill.invoice_line_ids.write({"analytic_distribution": dist((s["sales"], 50), (s["mkt"], 50))})
    bill.action_post()
    assert bill.state == "posted"
    with pytest.raises(UserError):
        bill.invoice_line_ids.write({"analytic_distribution": None})
    # income lines stay optional
    assert invoice(env, partner(env), [{"price_unit": 10.0}]).state == "posted"


def test_mandatory_plan_by_journal_and_prefix(env, setup):
    s = setup
    sale_journal = env["account.journal"].search(
        [("type", "=", "sale"), ("company_id", "=", env.company.ids[0])], limit=1
    )
    s["proj"].write(
        {"applicability_ids": [("create", {"journal_id": sale_journal.ids[0], "applicability": "mandatory"})]}
    )
    with pytest.raises(UserError):
        invoice(env, partner(env), [{"price_unit": 10.0}])
    assert (
        invoice(env, partner(env), [{"price_unit": 10.0, "analytic_distribution": dist((s["web"], 100))}]).state
        == "posted"
    )
    # a more specific optional rule (prefix) overrides the journal rule
    s["proj"].write(
        {
            "applicability_ids": [
                ("create", {"account_prefix": "40", "journal_id": sale_journal.ids[0], "applicability": "optional"})
            ]
        }
    )
    assert invoice(env, partner(env), [{"price_unit": 10.0}]).state == "posted"
    s["dept"].write({"default_applicability": "mandatory"})
    with pytest.raises(UserError):
        invoice(env, partner(env), [{"price_unit": 10.0}])


def test_access_rights(env, setup):
    s = setup
    clerk = env.with_user(_user(env, "billing-an@test", ["account.group_account_invoice"]).ids[0])
    move = invoice(clerk, partner(clerk), [{"price_unit": 10.0, "analytic_distribution": dist((s["sales"], 100))}])
    assert move.state == "posted"
    assert env["account.analytic_line"].search_count([("move_id", "=", move.ids[0])]) == 1
    assert clerk["account.analytic_account"].search_count([]) >= 3
    with pytest.raises(AccessError):
        clerk["account.analytic_account"].create({"name": "No", "plan_id": s["dept"].ids[0]})
    with pytest.raises(AccessError):
        clerk["account.analytic_line"].search([])
    auditor = env.with_user(_user(env, "auditor-an@test", ["account.group_account_readonly"]).ids[0])
    assert auditor["account.analytic_line"].search_count([("move_id", "=", move.ids[0])]) == 1
    with pytest.raises(AccessError):
        auditor["account.analytic_plan"].create({"name": "No"})
    plain = env.with_user(_user(env, "plain-an@test", ["core.group_user"]).ids[0])
    with pytest.raises(AccessError):
        plain["account.analytic_account"].search([])
    accountant = env.with_user(_user(env, "acc-an@test", ["account.group_account_user"]).ids[0])
    manual = accountant["account.analytic_line"].create(
        {"name": "Timesheet cost", "amount": -42.0, "account_id": s["mkt"].ids[0]}
    )
    assert manual.company_id == env.company


def test_company_isolation(env, setup):
    e = eu_env(env)
    eu_plan = plan(e, "EU cost centres", company_id=e.company.ids[0])
    eu_acc = analytic(e, eu_plan, "LYO", company_id=e.company.ids[0])
    eu_move = invoice(e, partner(e, "Lyon SA"), [{"price_unit": 10.0, "analytic_distribution": dist((eu_acc, 100))}])
    us_user = env.with_user(
        _user(env, "us-an@test", ["account.group_account_user"], env.ref("core.main_company")).ids[0]
    )
    assert not us_user["account.analytic_plan"].search([("id", "=", eu_plan.ids[0])])
    assert not us_user["account.analytic_account"].search([("id", "=", eu_acc.ids[0])])
    assert not us_user["account.analytic_line"].search([("move_id", "=", eu_move.ids[0])])
    assert us_user["account.analytic_plan"].search([("id", "=", setup["dept"].ids[0])])  # shared plan


def test_analytic_account_lifecycle_and_services(env, setup):
    s = setup
    invoice(env, partner(env), [{"price_unit": 10.0, "analytic_distribution": dist((s["sales"], 100))}])
    with pytest.raises(ValidationError):
        s["sales"].unlink()
    unused = analytic(env, s["dept"], "TMP")
    unused.unlink()
    assert s["sales"].display_name == "[SAL] Sales"
    assert s["dept"].account_count == 2
    from modules.account_analytic.services import allocation_context, validate_distribution

    ctx = allocation_context(env)
    names = {p["plan"]: [a["code"] for a in p["accounts"]] for p in ctx["plans"]}
    assert names["Departments"] == ["MKT", "SAL"] and names["Projects"] == ["WEB"]
    assert validate_distribution(env, {s["web"].ids[0]: 100}) == dist((s["web"], 100))
    with pytest.raises(ValidationError):
        validate_distribution(env, dist((s["web"], 101)))


def test_views_and_demo(env):
    from modules.account_analytic.demo import seed_company
    from modules.web.views import resolve_view

    form = resolve_view(env, "account.move", "form", "account.view_invoice_form")
    assert "analytic_distribution" in str(form)
    pivot = resolve_view(env, "account.analytic_line", "pivot", "account_analytic.view_analytic_line_pivot")
    assert pivot["col"] == "date:month"
    created = seed_company(env, env.company)
    assert created == 8
    assert seed_company(env, env.company) == 0
    items = env["account.analytic_line"].search([("company_id", "=", env.company.ids[0])])
    assert items and {i.plan_id.name for i in items} == {"Departments", "Projects"}
    rent = items.filtered(lambda i: i.general_account_id.code == "6100")
    assert rent and sorted({i.account_id.code for i in rent}) == ["ADM", "SAL"]
