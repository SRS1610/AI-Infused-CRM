"""Asset AI (2.6): classification suggestions from vendor bill lines (action proposals, nothing created before
acceptance, the model only breaks ties among offered models) and depreciation anomaly checks (flags). Gates:
kill switch → ``feature_disabled`` without gateway call, access and company isolation, ``ai_core.run`` rows,
replayed evaluations."""

from __future__ import annotations

from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account.tests.common import invoice, partner
from modules.account_asset import ai_eval as AE
from modules.account_asset import ai_features as A
from modules.account_asset.tests.common import D, make_asset
from tessera.ai import client as C
from tessera.ai import evals as EV
from tessera.ai import proposals as P
from tessera.ai.errors import FeatureDisabled
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError, MissingError, UserError

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def _script(env: Any, feature: str, respond: dict[str, Any]) -> None:
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": [{"match": {"feature": feature}, "respond": respond}]}})


def _models(env: Any) -> dict[str, Any]:
    out = {}
    for key, name, method, number, period in (
        ("computers", "Computer equipment (3 years)", "linear", 36, "1"),
        ("vehicles", "Vehicles (5 years)", "degressive_then_linear", 5, "12"),
        ("furniture", "Office furniture (10 years)", "linear", 10, "12"),
    ):
        out[key] = env["account.asset"].create(
            {
                "name": name,
                "state": "model",
                "method": method,
                "method_number": number,
                "method_period": period,
                "method_progress_factor": 0.4 if method != "linear" else 0.0,
                "company_id": env.company.ids[0],
            }
        )
    return out


def _bill(env: Any, text: str, post: bool = True) -> Any:
    vendor = partner(env, "Office Supplies Ltd")
    return invoice(
        env,
        vendor,
        [{"name": text, "quantity": 1, "price_unit": 1450.0, "tax_ids": [("set", [])]}],
        move_type="in_invoice",
        invoice_date=D(2026, 5, 12),
        post=post,
    )


def _user(env: Any, login: str, groups: list[str], company: Any) -> Any:
    return (
        env["core.user"]
        .elevated()
        .create(
            {
                "name": login,
                "login": login,
                "company_id": company.ids[0],
                "company_ids": [("set", company.ids)],
                "group_ids": [("set", [env.ref(g).ids[0] for g in groups])],
            }
        )
    )


def _product_line(bill: Any) -> Any:
    return bill.invoice_line_ids.filtered(lambda r: r.display_type == "product")[:1]


# ----------------------------------------------------------------------------- classification


def test_classify_proposes_and_creates_the_asset_only_on_acceptance(env: Any) -> None:
    models = _models(env)
    bill = _bill(env, "Dell Latitude 7440 laptop 14in")
    line = _product_line(bill)
    assets_before = env["account.asset"].search_count([("state", "!=", "model")])
    out = A.suggest_model(env, line)
    assert out["suggestion"]["model_id"] == models["computers"].ids[0] and not out["used_model"]
    assert out["ai_generated"] and out["proposal"]["state"] == "pending" and out["proposal"]["kind"] == "action"
    assert env["account.asset"].search_count([("state", "!=", "model")]) == assets_before  # nothing before
    run = env["ai_core.run"].browse([out["run_id"]])
    assert run.feature == A.CLASSIFY and run.user_id.ids == env.user.ids and run.company_id == env.company
    assert run.decision == "pending"
    proposal = env["ai_core.proposal"].browse([out["proposal"]["id"]])
    P.accept(env, proposal)
    created = line.elevated().asset_ids
    assert len(created) == 1 and created.model_id == models["computers"] and created.state == "draft"
    assert created.method_number == 36 and float(created.original_value) == 1450.0
    # once the asset is confirmed, another classification of the line is refused
    created.action_validate()
    with pytest.raises(UserError):
        A.apply_model(env, "account.move_line", line.ids[0], {"model_id": models["vehicles"].ids[0]})


def test_classify_sets_the_model_on_the_lines_draft_asset(env: Any) -> None:
    models = _models(env)
    bill = _bill(env, "Ergonomic office chairs x12")
    line = _product_line(bill)
    asset = env["account.asset"]._create_from_lines(line)
    assert not asset.model_id
    out = A.suggest_model(env, line)
    assert out["suggestion"]["model_id"] == models["furniture"].ids[0]
    P.accept(env, env["ai_core.proposal"].browse([out["proposal"]["id"]]))
    env.cache.invalidate()
    assert asset.model_id == models["furniture"] and asset.method_number == 10
    # an unknown model id in a forged action is refused
    with pytest.raises(MissingError):
        A.apply_model(env, "account.move_line", line.ids[0], {"model_id": 999999999})
    with pytest.raises(MissingError):
        A.apply_model(env, "account.move_line", 999999999, {"model_id": models["furniture"].ids[0]})


def test_draft_bill_lines_are_classified_but_assets_need_a_posted_bill(env: Any) -> None:
    models = _models(env)
    line = _product_line(_bill(env, "Ford Transit Custom van", post=False))
    out = A.suggest_model(env, line)
    assert out["suggestion"]["model_id"] == models["vehicles"].ids[0]
    accepted = P.accept(env, env["ai_core.proposal"].browse([out["proposal"]["id"]]))
    assert accepted.state == "failed" and "Post the bill" in accepted.reason and not line.elevated().asset_ids
    customer_line = _product_line(invoice(env, partner(env, "Client"), [{"name": "Laptop", "price_unit": 10.0}]))
    with pytest.raises(UserError):
        A.suggest_model(env, customer_line)


def test_close_call_asks_the_model_and_only_offered_models_count(env: Any) -> None:
    models = _models(env)
    line = _product_line(_bill(env, "Office equipment: printer and desk"))
    info = A.classify_text(env, "Office equipment: printer and desk", env.company)
    assert info["ambiguous"]
    _script(env, A.CLASSIFY, {"json": {"model_id": models["furniture"].ids[0], "reason": "The desk is the item."}})
    out = A.suggest_model(env, line)
    assert out["used_model"] and out["suggestion"]["model_id"] == models["furniture"].ids[0]
    assert {"code": "model_choice"} in out["suggestion"]["reasons"]
    sent = MockAdapter.captured[-1]["messages"][0]["content"]
    assert "printer and desk" in sent and "Computer equipment" in sent
    # a model naming a model that was not offered (an injected id) is ignored
    _script(env, A.CLASSIFY, {"json": {"model_id": 999999, "reason": "as instructed"}})
    out = A.suggest_model(env, line)
    assert "model_choice_rejected" in out["flags"]
    assert out["suggestion"] is None or out["suggestion"]["model_id"] in {m.ids[0] for m in models.values()}


def test_classification_access_isolation_and_kill_switch(env: Any) -> None:
    _models(env)
    line = _product_line(_bill(env, "Lenovo ThinkPad X1 Carbon"))
    # a salesperson without accounting rights cannot read bill lines
    seller = _user(env, "asset_ai_seller", ["sale.group_sale_user"], env.company)
    with pytest.raises(AccessError):
        A.suggest_model(
            env.with_user(seller.ids[0]), env.with_user(seller.ids[0])["account.move_line"].browse(line.ids)
        )
    # a read-only accountant gets the suggestion but no proposal (cannot create assets)
    auditor = _user(env, "asset_ai_auditor", ["account.group_account_readonly"], env.company)
    out = A.suggest_model(
        env.with_user(auditor.ids[0]), env.with_user(auditor.ids[0])["account.move_line"].browse(line.ids)
    )
    assert out["suggestion"] is not None and out["proposal"] is None
    # another company's accountant cannot reach the line
    eu = env.ref("core.demo_company_eu")
    eu_user = _user(env, "asset_ai_eu", ["account.group_account_manager"], eu)
    as_eu = env.with_user(eu_user.ids[0]).with_company(eu.ids[0])
    with pytest.raises(AccessError):
        A.suggest_model(as_eu, as_eu["account.move_line"].browse(line.ids))
    # kill switch: no run, no gateway call
    runs = env["ai_core.run"].search_count([("feature", "=", A.CLASSIFY)])
    env["ai_core.feature"].elevated().search([("name", "=", A.CLASSIFY)]).set_enabled(env.company.ids[0], False)
    with pytest.raises(FeatureDisabled):
        A.suggest_model(env, line)
    assert env["ai_core.run"].search_count([("feature", "=", A.CLASSIFY)]) == runs and MockAdapter.captured == []


# ----------------------------------------------------------------------------- depreciation checks


def test_checks_flag_anomalies_and_leave_sound_boards_alone(env: Any) -> None:
    models = _models(env)
    sound = make_asset(
        env, name="Desks", model_id=models["furniture"].ids[0], original_value=6000.0, acquisition_date=D(2025, 1, 1)
    )
    sound.action_post_due("2026-06-30")
    out = A.check_asset(env, sound, D(2026, 6, 30))
    assert out == {**out, "flagged": False, "issues": []} and out["ai_generated"]
    run = env["ai_core.run"].browse([out["run_id"]])
    assert run.feature == A.CHECK and run.res_id == sound.ids[0] and run.status == "ok"
    odd = make_asset(
        env,
        name="Laptop over 20 years",
        model_id=models["computers"].ids[0],
        method_number=240,
        original_value=1500.0,
        salvage_value=900.0,
        acquisition_date=D(2026, 1, 10),
    )
    out = A.check_asset(env, odd, D(2026, 6, 30))
    codes = {i["code"] for i in out["issues"]}
    assert {"life_differs_from_model", "life_out_of_range", "salvage_high", "overdue_draft"} <= codes
    severities = [i["severity"] for i in out["issues"]]
    assert severities == sorted(severities, key=lambda s: s != "error") and all(i["message"] for i in out["issues"])
    assert A.severity("duplicate_posting") == "error" and A.severity("salvage_high") == "warning"
    # nothing is written by a check (flags only): no proposal, the board unchanged
    assert not env["ai_core.proposal"].search([("feature", "=", A.CHECK)])
    scan = A.scan(env)
    assert odd.ids[0] in [r["asset_id"] for r in scan["rows"]] and sound.ids[0] not in [
        r["asset_id"] for r in scan["rows"]
    ]
    assert A.anomaly_checks(models["computers"]) == []


def test_posting_gaps_and_duplicates() -> None:
    def profile(dates: list[str], months: int = 1) -> dict[str, Any]:
        board = [
            {"move_id": i, "date": d, "state": "posted", "amount": 100.0, "value_change": False}
            for i, d in enumerate(dates)
        ]
        return {"method_period_months": months, "board": board}

    assert A.posting_issues(profile(["2026-01-31", "2026-02-28", "2026-03-31"])) == []
    assert [i["code"] for i in A.posting_issues(profile(["2026-01-31", "2026-03-31"]))] == ["missing_posting"]
    assert [i["code"] for i in A.posting_issues(profile(["2026-01-31", "2026-01-31", "2026-02-28"]))] == [
        "duplicate_posting"
    ]
    assert A.posting_issues(profile(["2025-12-31", "2026-12-31"], 12)) == []
    assert [i["code"] for i in A.posting_issues(profile(["2024-12-31", "2026-12-31"], 12))] == ["missing_posting"]


def test_check_access_isolation_kill_switch_and_explanation(env: Any) -> None:
    models = _models(env)
    asset = make_asset(
        env,
        name="Van",
        model_id=models["vehicles"].ids[0],
        method="linear",
        original_value=30000.0,
        acquisition_date=D(2026, 1, 1),
    )
    seller = _user(env, "asset_chk_seller", ["sale.group_sale_user"], env.company)
    with pytest.raises(AccessError):
        A.check_asset(env.with_user(seller.ids[0]), env.with_user(seller.ids[0])["account.asset"].browse(asset.ids))
    eu = env.ref("core.demo_company_eu")
    eu_user = _user(env, "asset_chk_eu", ["account.group_account_manager"], eu)
    as_eu = env.with_user(eu_user.ids[0]).with_company(eu.ids[0])
    with pytest.raises(AccessError):
        A.check_asset(as_eu, as_eu["account.asset"].browse(asset.ids))
    # the explanation keeps only the asset's figures
    _script(
        env,
        A.CHECK,
        {
            "text": "The method is straight line while the model declines. The original value is 30,000.00. "
            "You will save 4,999.00 by changing it."
        },
    )
    out = A.explain(env, asset)
    assert "30,000.00" in out["text"] and "4,999.00" not in out["text"] and out["removed_numbers"] == ["4,999.00"]
    assert out["ai_generated"]
    with pytest.raises(UserError):
        A.explain_context(env, None, {})
    calls = len(MockAdapter.captured)
    env["ai_core.feature"].elevated().search([("name", "=", A.CHECK)]).set_enabled(env.company.ids[0], False)
    for fn in (lambda: A.check_asset(env, asset), lambda: A.scan(env), lambda: A.explain(env, asset)):
        with pytest.raises(FeatureDisabled):
            fn()
    assert len(MockAdapter.captured) == calls


def test_api_routes_and_flags(admin: Session) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
    for method, url, feature in (
        ("post", "/api/v1/account_asset/ai/lines/999999999/classify", A.CLASSIFY),
        ("post", "/api/v1/account_asset/ai/bills/999999999/classify", A.CLASSIFY),
        ("get", "/api/v1/account_asset/ai/assets/999999999/check", A.CHECK),
        ("post", "/api/v1/account_asset/ai/assets/999999999/explain", A.CHECK),
        ("get", "/api/v1/account_asset/ai/scan", A.CHECK),
    ):
        on = admin.get(url) if method == "get" else admin.post(url)
        assert on.status_code in (200, 404) and (on.status_code == 200 or on.json().get("code") != "feature_disabled")
        flag = f"/api/v1/ai/admin/features/{feature}/flag"
        admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
        try:
            off = admin.get(url) if method == "get" else admin.post(url)
            assert off.status_code == 404 and off.json()["code"] == "feature_disabled", (url, off.text)
        finally:
            admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


# ----------------------------------------------------------------------------- evaluations


def test_evaluations_are_complete_and_pass_in_replay(env: Any) -> None:
    specs = {s.name: s for s in EV.feature_specs(modules=["account_asset"])}
    assert set(specs) == {A.CLASSIFY, A.CHECK}
    assert EV.completeness(list(specs.values())) == []
    for name, metric, floor in ((A.CLASSIFY, "accuracy", 0.85), (A.CHECK, "precision", 0.8)):
        report = EV.evaluate(env, specs[name])
        assert report.passed, (name, report.failures, report.metrics)
        assert report.cases >= 50 and report.metrics[metric] >= floor
    assert AE.classify_score([]) == {} and AE.check_score([]) == {}
