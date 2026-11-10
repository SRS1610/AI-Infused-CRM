"""Assets: models, assets from vendor bills, lifecycle rules, the posting job, services, access and demo."""

from __future__ import annotations

from typing import Any

import pytest

from modules.account.chart import chart_ref
from modules.account.tests.common import eu_env, invoice, partner
from modules.account_asset.tests.common import FAR, D, board, make_asset
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


def _model(env, **vals):
    return env["account.asset"].create(
        {
            "name": "Computers (3 years)",
            "state": "model",
            "method": "linear",
            "method_number": 3,
            "method_period": "12",
            "prorata_computation_type": "none",
            **vals,
        }
    )


def _bill(env, account, amount=2400.0, day=D(2026, 2, 10), **vals):
    return invoice(
        env,
        partner(env, "Hardware Store"),
        [{"name": "Laptop", "price_unit": amount, "account_id": account.ids[0], "tax_ids": [("set", [])]}],
        move_type="in_invoice",
        invoice_date=day,
        date=day,
        **vals,
    )


def test_defaults_from_chart_and_model(env):
    model = _model(env, method_number=4, method_period="1")
    assert not model.account_asset_id  # models only carry what is set on them
    asset = env["account.asset"].create(
        {"name": "Server", "model_id": model.ids[0], "original_value": 4800.0, "acquisition_date": D(2026, 3, 5)}
    )
    assert asset.method_number == 4 and asset.method_period == "1" and asset.prorata_computation_type == "none"
    assert asset.account_asset_id == chart_ref(env.company, "fixed_assets")
    assert asset.account_depreciation_id == chart_ref(env.company, "accumulated_depreciation")
    assert asset.journal_id == chart_ref(env.company, "journal_misc")
    assert asset.prorata_date == D(2026, 3, 5) and asset.state == "draft"
    asset.compute_depreciation_board()  # preview on a draft asset
    assert board(asset) == [
        ("2026-03-31", 1200.0),
        ("2026-04-30", 1200.0),
        ("2026-05-31", 1200.0),
        ("2026-06-30", 1200.0),
    ]
    asset.write({"original_value": 2400.0})  # the preview follows the values
    assert board(asset)[0] == ("2026-03-31", 600.0)
    asset.unlink()
    model.unlink()


def test_bill_creates_assets_from_account_model(env):
    model = _model(env)
    fixed = chart_ref(env.company, "fixed_assets")
    fixed.write({"create_asset": "validate", "asset_model_id": model.ids[0]})
    bill = _bill(env, fixed)
    line = bill.invoice_line_ids
    asset = line.asset_ids
    assert len(asset) == 1 and asset.state == "open" and asset.model_id == model
    assert float(asset.original_value) == 2400.0 and asset.acquisition_date == D(2026, 2, 10)
    assert asset.original_move_line_ids == line
    assert board(asset) == [("2026-12-31", 800.0), ("2027-12-31", 800.0), ("2028-12-31", 800.0)]
    with pytest.raises(UserError):
        line.action_create_asset()  # already has one
    fixed.write({"create_asset": "draft"})
    assert _bill(env, fixed, 900.0).invoice_line_ids.asset_ids.state == "draft"
    # customer invoices never create assets
    other = invoice(env, partner(env), [{"price_unit": 10.0, "account_id": fixed.ids[0]}])
    assert not other.invoice_line_ids.asset_ids


def test_create_assets_button(env):
    fixed = chart_ref(env.company, "fixed_assets")
    bill = _bill(env, fixed, 1500.0)
    assert not bill.invoice_line_ids.asset_ids
    ids = bill.action_create_assets()
    asset = env["account.asset"].browse(ids)
    assert asset.state == "draft" and float(asset.original_value) == 1500.0 and asset.name == "Laptop"
    with pytest.raises(UserError):
        bill.action_create_assets()  # nothing left
    expense_bill = _bill(
        env, env["account.account"].search([("code", "=", "6500"), ("company_id", "=", env.company.ids[0])])
    )
    with pytest.raises(UserError):
        expense_bill.action_create_assets()
    line = expense_bill.invoice_line_ids
    created = env["account.asset"].browse(line.action_create_asset(validate=True))
    assert created.state == "open"
    draft_bill = _bill(env, fixed, post=False)
    with pytest.raises(UserError):
        draft_bill.action_create_assets()
    with pytest.raises(UserError):
        draft_bill.invoice_line_ids.action_create_asset()


def test_validation_rules(env):
    with pytest.raises(ValidationError):
        make_asset(env, validate=False, original_value=100.0, salvage_value=200.0)
    with pytest.raises(ValidationError):
        make_asset(env, validate=False, original_value=100.0, method_number=0)
    with pytest.raises(ValidationError):
        make_asset(env, validate=False, original_value=100.0, method="degressive", method_progress_factor=1.5)
    with pytest.raises(UserError):
        make_asset(env, original_value=0.0)
    e = eu_env(env)
    with pytest.raises(ValidationError):
        make_asset(
            env, validate=False, original_value=10.0, account_asset_id=chart_ref(e.company, "fixed_assets").ids[0]
        )
    incomplete = env["account.asset"].create(
        {
            "name": "No accounts",
            "original_value": 10.0,
            "acquisition_date": D(2026, 1, 1),
            "company_id": env.company.ids[0],
        }
    )
    incomplete.write({"journal_id": None})
    with pytest.raises(UserError):
        incomplete.action_validate()


def test_lifecycle_rules(env):
    asset = make_asset(
        env,
        original_value=1200.0,
        acquisition_date=D(2026, 1, 1),
        method_number=12,
        method_period="1",
        prorata_computation_type="none",
    )
    with pytest.raises(UserError):
        asset.action_validate()  # already running
    with pytest.raises(UserError):
        asset.action_resume()
    asset.action_set_to_draft()
    assert asset.state == "draft" and not asset.depreciation_move_ids
    asset.action_validate()
    asset.action_post_due("2026-02-28")
    with pytest.raises(UserError):
        asset.action_set_to_draft()
    with pytest.raises(UserError):
        asset.write({"original_value": 2000.0})  # use Modify
    with pytest.raises(UserError):
        asset.unlink()
    posted = asset.depreciation_move_ids.filtered(lambda m: m.state == "posted")[0]
    with pytest.raises(UserError):
        posted.button_draft()
    with pytest.raises(UserError):
        posted.button_cancel()
    with pytest.raises(UserError):
        asset.action_pause("2026-01-15")  # before the last posted entry
    with pytest.raises(UserError):
        asset.action_modify(500.0, "2026-01-10")  # before the last posted entry
    with pytest.raises(UserError):
        asset.action_sell("2026-03-31")  # neither invoice line nor amount
    with pytest.raises(UserError):
        asset.action_sell("2026-03-31", amount=-1)
    asset.action_pause("2026-03-10")
    with pytest.raises(UserError):
        asset.action_pause()
    with pytest.raises(UserError):
        asset.action_modify(500.0, "2026-04-01")
    with pytest.raises(UserError):
        asset.compute_depreciation_board()
    # a paused asset can still be disposed of: no prorata for the paused time
    closing = env["account.move"].browse([asset.action_dispose("2026-05-31")])
    assert closing.state == "posted" and asset.state == "close"
    assert board(asset)[-1] == ("2026-03-10", 33.33)  # 1-10 March = 10/30 of 100
    with pytest.raises(UserError):
        asset.action_dispose("2026-06-30")


def test_modify_below_salvage_refused(env):
    asset = make_asset(
        env,
        original_value=1000.0,
        salvage_value=100.0,
        acquisition_date=D(2026, 1, 1),
        method_number=3,
        prorata_computation_type="none",
    )
    with pytest.raises(UserError):
        asset.action_modify(50.0, "2026-12-31")
    asset.action_modify(700.0, "2026-06-30", counterpart_account_id=None)
    assert float(asset.book_value) == 700.0


def test_posting_job_respects_lock_dates(env, system_env):
    asset = make_asset(
        env,
        original_value=1200.0,
        acquisition_date=D(2026, 1, 1),
        method_number=12,
        method_period="1",
        prorata_computation_type="none",
    )
    env.company.write({"fiscalyear_lock_date": D(2026, 2, 15)})
    from modules.account_asset.services import post_depreciation_job

    result = post_depreciation_job(system_env, date="2026-03-31")
    assert result["posted"] >= 3
    env.invalidate_all()
    posted = asset.depreciation_move_ids.filtered(lambda m: m.state == "posted").sorted(lambda m: m.date)
    # January's entry falls in the locked period: posted on the first open day
    assert [m.date for m in posted] == [D(2026, 2, 16), D(2026, 2, 28), D(2026, 3, 31)]
    assert env.ref("account_asset.sched_post_depreciation").job_name == "account_asset.post_depreciation"


def test_services(env):
    from modules.account_asset.services import asset_model_candidates, asset_profile, board_checks

    asset = make_asset(
        env,
        name="Forklift",
        original_value=3000.0,
        salvage_value=300.0,
        acquisition_date=D(2026, 1, 1),
        method="degressive",
        method_progress_factor=0.4,
        method_number=3,
        prorata_computation_type="none",
    )
    profile = asset_profile(asset)
    assert profile["method"] == "degressive" and profile["method_progress_factor"] == 0.4
    assert profile["method_period_months"] == 12 and profile["accounts"]["asset"]["code"] == "1500"
    assert [b["amount"] for b in profile["board"]] == [1080.0, 648.0, 972.0]  # 2 700 x 40 %, 1 620 x 40 %, rest
    assert profile["board"][-1]["remaining"] == 0.0
    assert board_checks(asset, today=D(2026, 6, 1)) == []
    issues = board_checks(asset, today=D(2027, 6, 1))
    assert [i["code"] for i in issues] == ["overdue_draft"]
    asset.depreciation_move_ids[0].with_context(_account_internal=True).write({"asset_depreciation_value": 1.0})
    assert "board_total" in [i["code"] for i in board_checks(asset, today=D(2026, 6, 1))]
    _model(env, name="Vehicles")
    names = [c["name"] for c in asset_model_candidates(env)]
    assert "Vehicles" in names


def test_access_and_isolation(env):
    asset = make_asset(env, original_value=100.0, acquisition_date=D(2026, 1, 1))
    auditor = env.with_user(_user(env, "auditor-as@test", ["account.group_account_readonly"]).ids[0])
    assert auditor["account.asset"].browse(asset.ids).name == "Test asset"
    with pytest.raises(AccessError):
        auditor["account.asset"].create({"name": "No"})
    plain = env.with_user(_user(env, "plain-as@test", ["core.group_user"]).ids[0])
    with pytest.raises(AccessError):
        plain["account.asset"].search([])
    # a billing user's bill on an asset account still creates the asset
    model = _model(env)
    fixed = chart_ref(env.company, "fixed_assets")
    fixed.write({"create_asset": "draft", "asset_model_id": model.ids[0]})
    clerk = env.with_user(_user(env, "billing-as@test", ["account.group_account_invoice"]).ids[0])
    bill = _bill(clerk, clerk["account.account"].browse(fixed.ids))
    assert env["account.move"].browse(bill.ids).invoice_line_ids.asset_ids
    e = eu_env(env)
    eu_asset = make_asset(e, original_value=50.0, acquisition_date=D(2026, 1, 1))
    us_user = env.with_user(
        _user(env, "us-as@test", ["account.group_account_user"], env.ref("core.main_company")).ids[0]
    )
    assert not us_user["account.asset"].search([("id", "=", eu_asset.ids[0])])


def test_views_and_demo(env):
    from modules.account_asset.demo import seed_company
    from modules.web.views import resolve_view

    form = resolve_view(env, "account.move", "form", "account.view_invoice_form")
    assert "action_create_assets" in str(form)
    account_form = resolve_view(env, "account.account", "form", "account.view_account_form")
    assert "asset_model_id" in str(account_form)
    assert seed_company(env, env.company) == 3
    assert seed_company(env, env.company) == 0
    assets = env["account.asset"].search([("company_id", "=", env.company.ids[0]), ("name", "like", "(demo)")])
    assert sorted(a.name for a in assets) == ["Delivery van (demo)", "Laptop (demo)", "Office furniture (demo)"]
    for asset in assets:
        states = set(asset.depreciation_move_ids.mapped("state"))
        assert asset.state == "open" and states == {"draft", "posted"}  # partly posted boards
    laptop = assets.filtered(lambda a: a.name == "Laptop (demo)")
    assert laptop.original_move_line_ids  # created from its vendor bill
    assert float(laptop.depreciated_value) > 0


def test_full_board_reaches_salvage_with_job(env, system_env):
    asset = make_asset(
        env,
        original_value=999.99,
        salvage_value=99.99,
        acquisition_date=D(2026, 1, 1),
        method_number=7,
        method_period="1",
        prorata_computation_type="daily_computation",
    )
    from modules.account_asset.services import post_due_depreciation

    post_due_depreciation(system_env, D(2100, 1, 1))
    env.invalidate_all()
    asset = env["account.asset"].browse(asset.ids)
    assert float(asset.book_value) == 99.99 and float(asset.value_residual) == 0.0
    assert not asset.depreciation_move_ids.filtered(lambda m: m.state == "draft")
    assert asset.action_post_due(FAR) == 0


def test_user_interface(env):
    from modules.account_analytic.tests.common import check_ui

    assert check_ui(env, "account_asset") > 3


def test_board_checks_flags_and_job_isolation(env, system_env):
    from modules.account_asset.services import board_checks, post_due_depreciation

    asset = make_asset(
        env, original_value=1000.0, acquisition_date=D(2026, 1, 1), method_number=2, prorata_computation_type="none"
    )
    first, second = asset.depreciation_move_ids.sorted(lambda m: m.date)
    first.with_context(_account_internal=True).write({"asset_depreciation_value": -5.0})
    second.action_post()  # posted while the earlier entry is still a draft
    codes = {i["code"] for i in board_checks(asset, today=D(2026, 1, 2))}
    assert {"negative_amount", "draft_before_posted", "board_total"} <= codes
    # a failing asset (deprecated expense account) does not stop the job for the others
    expense = env["account.account"].create(
        {"code": "6399", "name": "Old depreciation", "account_type": "expense_depreciation"}
    )
    broken = make_asset(
        env,
        name="Broken",
        original_value=100.0,
        acquisition_date=D(2026, 1, 1),
        method_number=1,
        prorata_computation_type="none",
        account_depreciation_expense_id=expense.ids[0],
    )
    good = make_asset(
        env,
        name="Good",
        original_value=100.0,
        acquisition_date=D(2026, 1, 1),
        method_number=1,
        prorata_computation_type="none",
    )
    expense.write({"deprecated": True})
    post_due_depreciation(system_env, D(2027, 1, 1))
    env.invalidate_all()
    assert set(good.depreciation_move_ids.mapped("state")) == {"posted"}
    assert set(broken.depreciation_move_ids.mapped("state")) == {"draft"}
