"""``account_analytic.allocation_suggest`` (2.6): distributions learned from similar earlier lines on top of the
distribution models, as write proposals (nothing changes before acceptance), and the gates — kill switch →
``feature_disabled``, access and company isolation, ``ai_core.run`` rows, evaluation."""

from __future__ import annotations

from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account_analytic import ai_allocation as A
from modules.account_analytic import ai_eval as AE
from tessera.ai import client as C
from tessera.ai import evals as EV
from tessera.ai import proposals as P
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


def _line(move: Any) -> Any:
    return move.invoice_line_ids.filtered(lambda r: r.display_type == "product")[:1]


def test_suggestion_from_history_is_a_proposal_applied_on_acceptance(env: Any) -> None:
    query, expected, _by_plan = AE.build(env, {"seed": 7, "rule": "alpha"})
    line = _line(query)
    assert not line.analytic_distribution
    out = A.suggest(env, line)
    assert out["suggestion"] == expected and out["source"] == "history" and out["confident"]
    assert {a["plan"] for a in out["accounts"]} == {"Departments (eval)", "Projects (eval)"}
    assert out["candidates"][0]["lines"] >= 2 and "partner" in out["candidates"][0]["reasons"]
    assert not line.analytic_distribution  # nothing before acceptance
    run = env["ai_core.run"].browse([out["run_id"]])
    assert run.feature == A.FEATURE and run.res_id == line.ids[0] and run.decision == "pending"
    P.accept(env, env["ai_core.proposal"].browse([out["proposal"]["id"]]))
    env.cache.invalidate()
    assert A.normalise_distribution(line.analytic_distribution) == expected
    again = A.suggest(env, line)
    assert again["proposal"] is None and again["current"] == expected


def test_models_keep_their_plans_and_history_fills_the_others(env: Any) -> None:
    query, expected, by_plan = AE.build(
        env, {"seed": 8, "rule": "alpha", "model": {"prefix": "41", "dist": {"marketing": 100}}}
    )
    out = A.suggest(env, _line(query))
    assert out["source"] == "models+history" and out["suggestion"] == expected
    assert set(by_plan) == {"departments", "projects"}


def test_cold_start_gives_no_suggestion(env: Any) -> None:
    cold, _expected, _bp = AE.build(env, {"seed": 9, "rule": "hosting", "cold": ["hosting"], "expected": {}})
    out = A.suggest(env, _line(cold))
    assert out["suggestion"] == {} and out["proposal"] is None and not out["confident"]


def test_similarity_and_words() -> None:
    f = {"product": 1, "partner": 2, "account": 3, "words": A.words("Trade fair hotel October"), "date": None}
    h = {"product": None, "partner": 2, "account": 3, "words": A.words("Trade fair booth travel"), "date": None}
    score, reasons = A._similarity(f, h)
    assert reasons == ["partner", "account", "label"] and 3.5 < score < 4.5
    assert A.words("Le bureau et la chaise") == {"bureau", "chaise"}


def test_access_isolation_and_kill_switch(env: Any) -> None:
    query, _expected, _bp = AE.build(env, {"seed": 10, "rule": "rent"})
    line = _line(query)
    seller = _user(env, "ana_ai_seller", ["sale.group_sale_user"], env.company)
    as_seller = env.with_user(seller.ids[0])
    with pytest.raises(AccessError):
        A.suggest(as_seller, as_seller["account.move_line"].browse(line.ids))
    eu = env.ref("core.demo_company_eu")
    eu_user = _user(env, "ana_ai_eu", ["account.group_account_manager"], eu)
    as_eu = env.with_user(eu_user.ids[0]).with_company(eu.ids[0])
    with pytest.raises(AccessError):
        A.suggest(as_eu, as_eu["account.move_line"].browse(line.ids))
    auditor = _user(env, "ana_ai_auditor", ["account.group_account_readonly"], env.company)
    as_auditor = env.with_user(auditor.ids[0])
    out = A.suggest(as_auditor, as_auditor["account.move_line"].browse(line.ids))
    assert out["suggestion"] and out["proposal"] is None  # read-only: the suggestion, no proposal
    with pytest.raises(UserError):
        A.suggest(env, query.line_ids.filtered(lambda r: r.display_type == "payment_term")[:1])
    with pytest.raises(UserError):
        A.explain_context(env, None, {})
    runs = env["ai_core.run"].search_count([("feature", "=", A.FEATURE)])
    env["ai_core.feature"].elevated().search([("name", "=", A.FEATURE)]).set_enabled(env.company.ids[0], False)
    for fn in (lambda: A.suggest(env, line), lambda: A.suggest_move(env, query)):
        with pytest.raises(FeatureDisabled):
            fn()
    assert env["ai_core.run"].search_count([("feature", "=", A.FEATURE)]) == runs and MockAdapter.captured == []


def test_whole_document_and_explanation_context(env: Any) -> None:
    query, expected, _bp = AE.build(env, {"seed": 11, "rule": "laptops"})
    rows = A.suggest_move(env, query)["rows"]
    assert len(rows) == 1 and rows[0]["suggestion"] == expected
    ctx = A.explain_context(env, _line(query), {})
    assert "History candidates" in ctx["sources"][0].text and "similar lines" in ctx["sources"][0].text


def test_api_flag(admin: Session) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
    assert admin.post("/api/v1/account_analytic/ai/lines/999999999/suggest").status_code == 404
    flag = f"/api/v1/ai/admin/features/{A.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        for url in (
            "/api/v1/account_analytic/ai/lines/999999999/suggest",
            "/api/v1/account_analytic/ai/moves/999999999/suggest",
        ):
            off = admin.post(url)
            assert off.status_code == 404 and off.json()["code"] == "feature_disabled", (url, off.text)
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


def test_evaluation_is_complete_and_a_sample_passes(env: Any, monkeypatch: Any) -> None:
    """The whole set runs with ``tessera ai eval run account_analytic.allocation_suggest``; the suite replays a
    sample that keeps every injection, model and cold-start case."""
    (spec,) = EV.feature_specs([A.FEATURE])
    assert EV.completeness([spec]) == []
    cases = spec.cases()
    assert len(cases) >= 50
    keep = {"injection", "model", "cold"}
    sample = [c for i, c in enumerate(cases) if i % 5 == 0 or keep & set(c["tags"])]
    monkeypatch.setattr(type(spec), "cases", lambda self: sample)
    report = EV.evaluate(env, spec)
    assert report.failures == [f"only {len(sample)} cases, at least 50 required"], (report.failures, report.metrics)
    assert report.metrics["exact_accuracy"] >= 0.8 and report.metrics["zero_mutations"] == 1.0
    assert report.metrics["injection_resistance"] == 1.0
    assert A.score([]) == {} and AE.score([]) == {}
