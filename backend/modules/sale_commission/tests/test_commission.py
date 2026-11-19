"""Commission rules, bases, accelerators, assignments, statements, payout and access."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from modules.account.chart import chart_ref
from modules.account.payments import register_payments
from modules.account.tests.common import eu_env, invoice, partner
from modules.sale_commission.engine import paid_date, rule_for
from modules.sale_commission.models.plan import period_bounds, period_label
from tessera.exceptions import AccessError, UserError, ValidationError

pytestmark = pytest.mark.db
D = dt.date


def _user(env: Any, login: str, groups: list[str] | None = None, company: Any | None = None) -> Any:
    company = company or env.company
    return env["core.user"].create(
        {
            "name": login,
            "login": login,
            "company_id": company.ids[0],
            "company_ids": [("set", company.ids)],
            "group_ids": [("set", [env.ref(g).ids[0] for g in groups or ["sale.group_sale_user"]])],
        }
    )


def _plan(env: Any, users: list[Any], **vals: Any) -> Any:
    return env["sale.commission_plan"].create(
        {
            "name": "Test plan",
            "target_amount": vals.pop("target", 0),
            "rule_ids": [("create", {"applies_to": "all", "rate": 10})],
            "user_ids": [("create", {"user_id": u.ids[0]}) for u in users],
            **vals,
        }
    )


def _product(env: Any, name: str = "Commission widget", **vals: Any) -> Any:
    return env["product.product"].create({"name": name, "list_price": 100, "taxes_id": [("set", [])], **vals})


def _inv(
    env: Any, seller: Any, amount: float, day: D, *, qty: float = 1, product: Any = None, post: bool = True, **vals: Any
) -> Any:
    line = {"price_unit": amount, "quantity": qty, "tax_ids": [("set", [])]}
    if product is not None:
        line["product_id"] = product.ids[0]
    return invoice(
        env,
        vals.pop("customer", None) or partner(env),
        [line],
        invoice_date=day,
        invoice_user_id=seller.ids[0],
        post=post,
        **vals,
    )


def _statement(plan: Any, day: D) -> Any:
    return plan.env["sale.commission_statement"].browse(
        plan.action_generate_statements(day.isoformat())["statement_ids"]
    )


def test_periods() -> None:
    assert period_bounds("monthly", D(2026, 2, 14)) == (D(2026, 2, 1), D(2026, 2, 28))
    assert period_bounds("quarterly", D(2026, 11, 3)) == (D(2026, 10, 1), D(2026, 12, 31))
    assert period_bounds("quarterly", D(2026, 5, 3)) == (D(2026, 4, 1), D(2026, 6, 30))
    assert (
        period_label("monthly", D(2026, 2, 1)) == "2026-02" and period_label("quarterly", D(2026, 10, 1)) == "2026-Q4"
    )


def test_invoiced_basis_credit_notes_and_idempotent_recompute(env: Any) -> None:
    seller = _user(env, "com-a@test")
    plan = _plan(env, [seller])
    a = _inv(env, seller, 1000, D(2026, 9, 10))
    _inv(env, seller, 500, D(2026, 8, 31))  # another month
    _inv(env, seller, 700, D(2026, 9, 12), post=False)  # draft
    other = _user(env, "com-b@test")
    _inv(env, other, 900, D(2026, 9, 12))
    credit = env["account.move"].browse(a.action_reverse(date="2026-10-02"))
    credit.action_post()
    sept = _statement(plan, D(2026, 9, 15))
    mine = sept.filtered(lambda s: s.user_id == seller)
    assert len(sept) == 1 and mine.commission_amount == 100 and mine.base_amount == 1000
    assert mine.line_ids.move_id == a and mine.line_ids.date == D(2026, 9, 10)
    assert mine.name.endswith("2026-09") and mine.multiplier == 1 and mine.achievement == 0
    october = _statement(plan, D(2026, 10, 5))
    assert october.commission_amount == -100  # credit notes count when posted, salesperson of the invoice
    assert october.line_ids.move_type == "out_refund" and credit.invoice_user_id != seller
    # recompute: same result, and new documents of the period appear
    lines = mine.line_ids.ids
    mine.action_compute()
    assert mine.commission_amount == 100 and len(mine.line_ids) == 1 and mine.line_ids.ids != lines
    _inv(env, seller, 300, D(2026, 9, 20))
    again = _statement(plan, D(2026, 9, 1))
    assert again == mine and mine.commission_amount == 130


def test_rule_specificity_fixed_amounts_and_categories(env: Any) -> None:
    seller = _user(env, "com-rules@test")
    parent = env["product.category"].create({"name": "Machines"})
    child = env["product.category"].create({"name": "Robots", "parent_id": parent.ids[0]})
    robot = _product(env, "Robot X", category_id=child.ids[0])
    machine = _product(env, "Press", category_id=parent.ids[0])
    loose = _product(env, "Loose part")
    plan = _plan(env, [seller])
    plan.write(
        {
            "rule_ids": [
                ("create", {"applies_to": "category", "category_id": parent.ids[0], "rate": 4, "sequence": 5}),
                ("create", {"applies_to": "product", "product_id": robot.ids[0], "rate": 1, "fixed_amount": 25}),
            ]
        }
    )
    assert rule_for(plan.rule_ids, robot).rate == 1
    assert rule_for(plan.rule_ids, machine).rate == 4
    assert rule_for(plan.rule_ids, _product(env, "Child cat", category_id=child.ids[0])).rate == 4  # parent category
    assert rule_for(plan.rule_ids, loose).rate == 10
    _inv(env, seller, 1000, D(2026, 9, 3), qty=2, product=robot)  # 2000 x 1 % + 2 x 25
    _inv(env, seller, 1000, D(2026, 9, 4), product=machine)  # 4 %
    _inv(env, seller, 1000, D(2026, 9, 5), product=loose)  # 10 %
    st = _statement(plan, D(2026, 9, 1))
    assert sorted(st.line_ids.mapped("amount")) == [40, 70, 100]
    with pytest.raises(ValidationError, match="product category"):
        env["sale.commission_rule"].create({"plan_id": plan.ids[0], "applies_to": "category"})
    with pytest.raises(ValidationError, match="product of the rate"):
        env["sale.commission_rule"].create({"plan_id": plan.ids[0], "applies_to": "product"})
    with pytest.raises(ValidationError):
        env["sale.commission_rule"].create({"plan_id": plan.ids[0], "rate": 120})
    # without any matching rule a line earns nothing but counts in the base
    bare = env["sale.commission_plan"].create(
        {"name": "No rules", "user_ids": [("create", {"user_id": _user(env, "com-bare@test").ids[0]})]}
    )
    _inv(env, bare.user_ids.user_id, 500, D(2026, 9, 9))
    st = _statement(bare, D(2026, 9, 1))
    assert st.base_amount == 500 and st.commission_amount == 0 and not st.line_ids.rule_id


def test_accelerators_and_rounding(env: Any) -> None:
    seller = _user(env, "com-acc@test")
    plan = _plan(env, [seller], target=1000)
    plan.write(
        {
            "target_ids": [
                ("create", {"min_achievement": 0, "multiplier": 0.5}),
                ("create", {"min_achievement": 80, "multiplier": 1}),
                ("create", {"min_achievement": 100, "multiplier": 1.5}),
            ]
        }
    )
    _inv(env, seller, 800, D(2026, 9, 3))
    st = _statement(plan, D(2026, 9, 1))
    assert (st.achievement, st.multiplier, st.commission_amount) == (80, 1, 80)  # bands include their threshold
    _inv(env, seller, 200.05, D(2026, 9, 4))  # 20.005 x 1.5 = 30.0075
    st.action_compute()
    assert st.multiplier == 1.5 and st.target_amount == 1000
    assert sorted(st.line_ids.mapped("amount")) == [30.01, 120]  # half up, per line
    assert st.raw_amount == 100.01 and st.commission_amount == 150.01
    _inv(env, seller, 500, D(2026, 9, 5), move_type="out_refund")  # a large credit note: 50 %
    st.action_compute()
    assert st.multiplier == 0.5 and st.achievement == 50.01
    with pytest.raises(ValidationError):
        plan.write({"target_ids": [("create", {"min_achievement": 80, "multiplier": 2})]})
    # an assignment's own target replaces the plan's
    plan.user_ids.write({"target_amount": 100})
    st.action_compute()
    assert st.target_amount == 100 and st.multiplier == 1.5


def test_paid_basis(env: Any) -> None:
    seller = _user(env, "com-paid@test")
    plan = _plan(env, [seller], basis="paid")
    paid = _inv(env, seller, 1000, D(2026, 8, 20))
    unpaid = _inv(env, seller, 400, D(2026, 9, 2))
    assert paid_date(unpaid) is None
    register_payments(paid, date=D(2026, 9, 5))
    partly = _inv(env, seller, 600, D(2026, 9, 3))
    register_payments(partly, date=D(2026, 9, 6), amount=100)
    assert paid_date(paid) == D(2026, 9, 5)
    # a credit note of a paid invoice counts; of an unpaid one, never
    credit_paid = env["account.move"].browse(paid.action_reverse(date="2026-09-20"))
    credit_paid.write({"invoice_line_ids": [("update", credit_paid.invoice_line_ids.ids[0], {"price_unit": 100})]})
    credit_paid.action_post()
    credit_unpaid = env["account.move"].browse(unpaid.action_reverse(date="2026-09-21"))
    credit_unpaid.action_post()
    standalone = _inv(env, seller, 50, D(2026, 9, 22), move_type="out_refund")
    st = _statement(plan, D(2026, 9, 1))
    assert sorted((line.move_id.ids[0], line.amount) for line in st.line_ids) == sorted(
        [(paid.ids[0], 100), (credit_paid.ids[0], -10), (standalone.ids[0], -5)]
    )
    assert st.line_ids.filtered(lambda r: r.move_id == paid).date == D(2026, 9, 5)
    august = _statement(plan, D(2026, 8, 1))
    assert not august  # nothing was paid in August


def test_team_assignments_and_date_ranges(env: Any) -> None:
    a, b = _user(env, "com-team-a@test"), _user(env, "com-team-b@test")
    team = env["crm.team"].create(
        {
            "name": "Commission team",
            "member_ids": [("create", {"user_id": a.ids[0]}), ("create", {"user_id": b.ids[0]})],
        }
    )
    plan = env["sale.commission_plan"].create(
        {
            "name": "Team plan",
            "rule_ids": [("create", {"applies_to": "all", "rate": 10})],
            "user_ids": [("create", {"team_id": team.ids[0], "date_from": "2026-09-10", "date_to": "2026-09-30"})],
        }
    )
    _inv(env, a, 100, D(2026, 9, 5))  # before the assignment
    _inv(env, a, 200, D(2026, 9, 15))
    _inv(env, b, 300, D(2026, 9, 16))
    sts = _statement(plan, D(2026, 9, 1))
    assert {(s.user_id.login, s.commission_amount) for s in sts} == {("com-team-a@test", 20), ("com-team-b@test", 30)}
    assert not _statement(plan, D(2026, 11, 1))
    with pytest.raises(ValidationError, match="either a salesperson or a sales team"):
        env["sale.commission_plan_user"].create({"plan_id": plan.ids[0]})
    with pytest.raises(ValidationError, match="ends before"):
        env["sale.commission_plan_user"].create(
            {"plan_id": plan.ids[0], "user_id": a.ids[0], "date_from": "2026-05-02", "date_to": "2026-05-01"}
        )
    # one plan per salesperson and company at a time
    first = _plan(env, [_user(env, "com-twice@test")])
    twice = first.user_ids.user_id
    with pytest.raises(ValidationError, match="already in the plan"), env.cr.savepoint():
        _plan(env, [twice])
    other = env["sale.commission_plan"].create({"name": "Later plan"})
    first.user_ids.write({"date_to": "2026-06-30"})
    env["sale.commission_plan_user"].create(
        {"plan_id": other.ids[0], "user_id": twice.ids[0], "date_from": "2026-07-01"}
    )


def test_statement_workflow_bill_and_payment(env: Any) -> None:
    seller = _user(env, "com-pay@test")
    plan = _plan(env, [seller])
    _inv(env, seller, 1000, D(2026, 9, 3))
    st = _statement(plan, D(2026, 9, 1))
    st.write({"adjustment_amount": 15.5, "adjustment_note": "Spiff"})
    assert st.total_amount == 115.5
    with pytest.raises(UserError, match="Confirm the statement"):
        st.action_create_bill()
    st.action_confirm()
    with pytest.raises(UserError, match="Only draft"):
        st.action_compute()
    with pytest.raises(UserError, match="adjusted"):
        st.write({"adjustment_amount": 1})
    with pytest.raises(UserError, match="Only draft statements can be deleted"):
        st.unlink()
    with pytest.raises(UserError, match="Only draft statements can be confirmed"):
        st.action_confirm()
    st.action_draft()
    st.action_confirm()
    draft_bill = env["account.move"].browse(st.action_create_bill()["bill_ids"])
    draft_bill.button_cancel()  # a cancelled bill releases the statement
    assert st.state == "confirmed" and not st.bill_id
    bill = env["account.move"].browse(st.action_create_bill()["bill_ids"])
    assert bill.move_type == "in_invoice" and bill.partner_id == seller.partner_id and bill.amount_total == 115.5
    assert bill.invoice_line_ids.account_id == chart_ref(env.company, "commissions")
    assert st.bill_id == bill and st.state == "confirmed"
    with pytest.raises(UserError, match="already has a bill"):
        st.action_create_bill()
    with pytest.raises(UserError, match="without a bill"):
        st.action_draft()
    # a statement is generated again: left alone once confirmed
    assert _statement(plan, D(2026, 9, 1)) == st and st.state == "confirmed"
    bill.action_post()
    register_payments(bill, date=D(2026, 10, 5))
    assert bill.payment_state == "paid" and st.state == "paid"


def test_bill_rules(env: Any) -> None:
    seller = _user(env, "com-neg@test")
    plan = _plan(env, [seller])
    _inv(env, seller, 100, D(2026, 9, 3), move_type="out_refund")
    st = _statement(plan, D(2026, 9, 1))
    st.action_confirm()
    with pytest.raises(UserError, match="nothing to pay"):
        st.action_create_bill()
    # grouping: two statements of one person on one bill
    payee = _user(env, "com-two@test")
    p1 = _plan(env, [payee], period="monthly")
    _inv(env, payee, 100, D(2026, 9, 3))
    _inv(env, payee, 200, D(2026, 10, 3))
    both = _statement(p1, D(2026, 9, 1)) | _statement(p1, D(2026, 10, 1))
    both.action_confirm()
    bills = env["account.move"].browse(both.action_create_bill()["bill_ids"])
    assert len(bills) == 1 and len(bills.invoice_line_ids) == 2 and bills.amount_total == 30
    # a salesperson cannot pay commissions
    clerk = env.with_user(_user(env, "com-clerk@test", ["sale.group_sale_user"]).ids[0])
    with pytest.raises(AccessError):
        clerk["sale.commission_statement"].browse(both.ids).action_create_bill()


def test_statement_facts(env: Any) -> None:
    seller = _user(env, "com-facts@test")
    cat = env["product.category"].create({"name": "Facts cat"})
    item = _product(env, "Facts product", category_id=cat.ids[0])
    plan = _plan(env, [seller], target=1000)
    plan.write({"target_ids": [("create", {"min_achievement": 100, "multiplier": 2})]})
    _inv(env, seller, 1500, D(2026, 9, 3), product=item)
    _inv(env, seller, 100, D(2026, 9, 4), move_type="out_refund")
    st = _statement(plan, D(2026, 9, 1))
    facts = st.facts()
    assert facts["base_total"] == 1400 and facts["achievement_percent"] == 140 and facts["multiplier"] == 2
    assert facts["band"] == {"from_achievement": 100, "multiplier": 2}
    assert facts["commission"] == 280 and facts["commission_before_multiplier"] == 140 and facts["total"] == 280
    assert facts["credit_notes"] == {"count": 1, "base": -100.0, "commission": -20.0}
    assert {c["category"]: c["base"] for c in facts["by_category"]} == {"Facts cat": 1500.0, "-": -100.0}
    assert facts["rates"] == [{"applies_to": "all", "target": "All products", "rate": 10, "fixed_per_unit": 0}]
    assert (
        facts["documents"] == 2 and facts["period"] == "2026-09" and facts["currency"] == env.company.currency_id.name
    )


def test_access_rules_and_my_commissions(env: Any) -> None:
    a, b = _user(env, "com-own-a@test"), _user(env, "com-own-b@test")
    plan = _plan(env, [a, b])
    _inv(env, a, 100, D(2026, 9, 3))
    _inv(env, b, 100, D(2026, 9, 3))
    sts = _statement(plan, D(2026, 9, 1))
    as_a = env.with_user(a.ids[0])
    assert as_a["sale.commission_statement"].search([("plan_id", "=", plan.ids[0])]).user_id == a
    assert as_a["sale.commission_statement_line"].search([("statement_id", "in", sts.ids)]).statement_id.user_id == a
    with pytest.raises(AccessError):
        as_a["sale.commission_statement"].browse(sts.ids).action_confirm()
    with pytest.raises(AccessError):
        as_a["sale.commission_plan"].browse(plan.ids).action_generate_statements("2026-09-01")
    with pytest.raises(AccessError):
        as_a["sale.commission_plan"].create({"name": "Mine"})
    assert as_a["sale.commission_plan"].browse(plan.ids).name == "Test plan"  # salespeople read the plan
    manager = env.with_user(_user(env, "com-mgr@test", ["sale.group_sale_manager"]).ids[0])
    assert manager["sale.commission_statement"].search_count([("plan_id", "=", plan.ids[0])]) == 2
    accountant = env.with_user(_user(env, "com-acct@test", ["account.group_account_invoice"]).ids[0])
    assert accountant["sale.commission_statement"].search_count([("plan_id", "=", plan.ids[0])]) == 2
    plain = env.with_user(_user(env, "com-plain@test", ["core.group_user"]).ids[0])
    with pytest.raises(AccessError):
        plain["sale.commission_statement"].search([])
    action = env.ref("sale_commission.action_my_statements")
    assert action.domain == [["user_id", "=", "$uid"]]


def test_company_isolation(env: Any) -> None:
    eu = eu_env(env)
    seller = _user(env, "com-eu@test", company=eu.company)
    eu_plan = _plan(eu, [seller], company_id=eu.company.ids[0])
    _inv(eu, seller, 100, D(2026, 9, 3))
    st = _statement(eu_plan, D(2026, 9, 1))
    assert st.company_id == eu.company and st.currency_id == eu.company.currency_id and st.commission_amount == 10
    us = env.with_user(_user(env, "com-us-mgr@test", ["sale.group_sale_manager"], env.ref("core.main_company")).ids[0])
    assert not us["sale.commission_plan"].search([("id", "=", eu_plan.ids[0])])
    assert not us["sale.commission_statement"].search([("id", "=", st.ids[0])])
    assert not us["sale.commission_statement_line"].search([("statement_id", "=", st.ids[0])])
    assert not us["sale.commission_rule"].search([("plan_id", "=", eu_plan.ids[0])])
    # a US plan does not see the EU company's invoices
    us_plan = _plan(env, [seller])
    assert not _statement(us_plan, D(2026, 9, 1))  # no statement without anything to count


def test_demo_hook(system_env: Any) -> None:
    from modules.sale_commission.demo import seed_commission_demo

    seed_commission_demo(system_env)
    sts = system_env["sale.commission_statement"].search(
        [("plan_id", "=", system_env.ref("sale_commission.plan_demo").ids[0])]
    )
    assert {s.user_id.login for s in sts} == {"hugo", "nadia"}
