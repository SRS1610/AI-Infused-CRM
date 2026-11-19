"""``account_tax.prefiling`` (2.6): ranked and explained pre-filing findings with statistical outliers against the
previous periods (flags; nothing written, closing never blocked), the grounded briefing, and the gates — kill switch
→ ``feature_disabled`` without gateway call, access and company isolation, ``ai_core.run`` rows, evaluation."""

from __future__ import annotations

from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account_tax import ai_eval as AE
from modules.account_tax import ai_prefiling as A
from modules.account_tax.tests.common import Q2, eu_golden
from tessera.ai import client as C
from tessera.ai import evals as EV
from tessera.ai.errors import FeatureDisabled
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


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


def _eu(env: Any) -> Any:
    eu = env.ref("core.demo_company_eu")
    env.user.elevated().write({"company_ids": [("add", eu.ids[0])]})
    return env.with_company(eu.ids[0])


def test_clean_golden_period_has_nothing_to_review(env: Any) -> None:
    golden = eu_golden(env)
    e = golden["env"]
    ret = e["account.tax_return"].create({"company_id": golden["company"].ids[0], "date_from": Q2[0], "date_to": Q2[1]})
    out = A.review(e, ret)
    assert out["items"] == [] and out["net"] == 130.0 and out["ai_generated"]
    run = e["ai_core.run"].browse([out["run_id"]])
    assert run.feature == A.FEATURE and run.res_model == "account.tax_return" and run.res_id == ret.ids[0]
    with pytest.raises(UserError):
        A.prepare(e, None, {})


def test_seeded_rate_outlier_is_ranked_and_explained_and_nothing_is_written(env: Any) -> None:
    e = _eu(env)
    ret, seeded = AE.build(e, {"seed": 4242, "anomaly": "rate_domestic_10"})
    returns = e["account.tax_return"].search_count([])
    out = A.review(e, ret)
    codes = [i["code"] for i in out["items"]]
    assert "rate_outlier_for_country" in codes
    item = next(i for i in out["items"] if i["code"] == "rate_outlier_for_country")
    assert seeded[0] in item["res_ids"] and item["source"] == "statistics" and item["why"] and item["action"]
    outlier = item["details"]["outliers"][0]
    assert outlier["rate"] == 10.0 and outlier["usual_rate"] == 20.0 and outlier["country"]
    assert [i["rank"] for i in out["items"]] == list(range(1, len(codes) + 1))
    # flags only: no new return, no proposal, the return not closed
    assert e["account.tax_return"].search_count([]) == returns and ret.state == "draft"
    assert not e["ai_core.proposal"].search([("feature", "=", A.FEATURE)])


def test_grid_swings_and_ranking() -> None:
    items = [
        {"code": "draft_entries", "severity": "warning", "count": 1, "details": {}},
        {"code": "report_ledger_mismatch", "severity": "error", "count": 0, "details": {"difference": 350.0}},
        {"code": "grid_swing", "severity": "warning", "count": 2, "details": {"swings": [{"change": 900.0}]}},
        {"code": "period_locked", "severity": "info", "count": 1, "details": {}},
    ]
    ranked = A.rank(items, 1000.0)
    assert [i["code"] for i in ranked] == ["report_ledger_mismatch", "grid_swing", "draft_entries", "period_locked"]
    assert all(i["title"] and i["why"] and i["action"] for i in ranked)


def test_swing_detected_on_a_scenario(env: Any) -> None:
    e = _eu(env)
    ret, _seeded = AE.build(e, {"seed": 777, "anomaly": "swing_up"})
    swings = A.grid_swings(e, ret.company_id, ret.date_from, ret.date_to)
    assert swings and any(s["kind"] == "increase" for s in swings)
    assert all(s["history"] and len(s["history"]) == A.HISTORY_PERIODS for s in swings)


def test_briefing_keeps_only_figures_of_the_findings(env: Any) -> None:
    e = _eu(env)
    ret, _seeded = AE.build(e, {"seed": 99, "anomaly": "manual_tax_entry"})
    p = e.ref("ai_core.provider_local")
    text = (
        "Fix the manual entry on the VAT account first: the return differs from the ledger by 350.0. "
        "This will cost you 12,345.00 in penalties."
    )
    p.write({"options": {**(p.options or {}), "script": [
        {"match": {"feature": A.FEATURE}, "respond": {"json": {"narrative": text}}}]}})  # fmt: skip
    out = A.narrate(e, ret)
    assert "12,345.00" not in out["narrative"] and out["removed_numbers"] == ["12,345.00"]
    assert "350.0" in out["narrative"] and out["ai_generated"]
    sent = MockAdapter.captured[-1]["messages"][0]["content"]
    assert "Return does not tie to the tax accounts" in sent


def test_access_isolation_and_kill_switch(env: Any) -> None:
    golden = eu_golden(env)
    e = golden["env"]
    ret = e["account.tax_return"].create({"company_id": golden["company"].ids[0], "date_from": Q2[0], "date_to": Q2[1]})
    seller = _user(env, "tax_ai_seller", ["sale.group_sale_user"], golden["company"])
    as_seller = env.with_user(seller.ids[0]).with_company(golden["company"].ids[0])
    with pytest.raises(AccessError):
        A.review(as_seller, as_seller["account.tax_return"].browse(ret.ids))
    other = _user(env, "tax_ai_other", ["account.group_account_manager"], env.ref("core.main_company"))
    as_other = env.with_user(other.ids[0])
    with pytest.raises(AccessError):
        A.review(as_other, as_other["account.tax_return"].browse(ret.ids))
    auditor = _user(env, "tax_ai_auditor", ["account.group_account_readonly"], golden["company"])
    as_auditor = env.with_user(auditor.ids[0]).with_company(golden["company"].ids[0])
    assert A.review(as_auditor, as_auditor["account.tax_return"].browse(ret.ids))["items"] == []
    runs = e["ai_core.run"].search_count([("feature", "=", A.FEATURE)])
    e["ai_core.feature"].elevated().search([("name", "=", A.FEATURE)]).set_enabled(golden["company"].ids[0], False)
    for fn in (lambda: A.review(e, ret), lambda: A.narrate(e, ret)):
        with pytest.raises(FeatureDisabled):
            fn()
    assert e["ai_core.run"].search_count([("feature", "=", A.FEATURE)]) == runs and MockAdapter.captured == []


def test_api_flag(admin: Session) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
    assert admin.get("/api/v1/account_tax/ai/returns/999999999/prefiling").status_code == 404
    flag = f"/api/v1/ai/admin/features/{A.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        for method, url in (
            ("get", "/api/v1/account_tax/ai/returns/999999999/prefiling"),
            ("post", "/api/v1/account_tax/ai/returns/999999999/prefiling/narrative"),
        ):
            off = admin.get(url) if method == "get" else admin.post(url)
            assert off.status_code == 404 and off.json()["code"] == "feature_disabled", (url, off.text)
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


def test_evaluation_is_complete_and_a_sample_passes(env: Any, monkeypatch: Any) -> None:
    """The whole set (64 scenarios, ~14 min) runs with ``tessera ai eval run account_tax.prefiling``; the suite
    replays a sample with every injection case and both kinds of clean quarters."""
    (spec,) = EV.feature_specs([A.FEATURE])
    assert EV.completeness([spec]) == []
    cases = spec.cases()
    assert len(cases) >= 50 and sum("injection" in c["tags"] for c in cases) >= 4
    sample = [c for i, c in enumerate(cases) if i % 8 == 0 or "injection" in c["tags"] or "level" in c["tags"]]
    monkeypatch.setattr(type(spec), "cases", lambda self: sample)
    report = EV.evaluate(env, spec)
    assert report.failures == [f"only {len(sample)} cases, at least 50 required"], (report.failures, report.metrics)
    assert report.metrics["recall"] >= 0.9 and report.metrics["injection_resistance"] == 1.0
    assert report.metrics["explained"] == 1.0
    assert AE.score([]) == {}
