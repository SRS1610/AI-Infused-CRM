"""``account_budget.variance_explain`` gates (2.6): only the budget-vs-actual report's figures survive in the
explanation, nothing is written, kill switch → ``feature_disabled`` without gateway call or run, permission
scoping and company isolation, the ``ai_core.run`` audit row, and the replayed evaluation."""

from __future__ import annotations

from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account.tests.common import D, invoice, partner
from modules.account_analytic.tests.common import account, analytic, dist, plan
from modules.account_budget import ai_features as A
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


def _script(env: Any, explanation: str) -> None:
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": [
        {"match": {"feature": A.FEATURE}, "respond": {"json": {"explanation": explanation}}}]}})  # fmt: skip


def _budget(env: Any) -> Any:
    mkt = analytic(env, plan(env, "Departments"), "MKT", "Marketing")
    budget = env["account.budget"].create(
        {
            "name": "Budget 2026",
            "date_from": D(2026, 1, 1),
            "date_to": D(2026, 12, 31),
            "line_ids": [
                ("create", {"name": "Marketing", "analytic_account_id": mkt.ids[0], "planned_amount": 1000.0})
            ],
        }
    )
    line = {"price_unit": 1250.0, "account_id": account(env, "6500").ids[0], "analytic_distribution": dist((mkt, 100))}
    invoice(
        env, partner(env, "Vendor"), [line], move_type="in_invoice", invoice_date=D(2026, 3, 10), date=D(2026, 3, 10)
    )
    return budget


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


def test_only_report_figures_survive_and_nothing_is_written(env: Any) -> None:
    budget = _budget(env)
    _script(env, "Marketing spent 1,250.00 against 1,000.00 planned. Next year it will reach 2,600.00.")
    before = (budget.write_date, budget.state)
    result = A.explain(env, budget, {"as_of": "2026-07-02"})
    assert result.data["removed_numbers"] == ["2,600.00"] and "2,600.00" not in result.text
    assert "1,250.00" in result.text and result.data["as_of"] == "2026-07-02"
    assert "numbers_removed" in result.flags and not result.proposals and result.to_dict()["ai_generated"]
    assert (budget.write_date, budget.state) == before
    run = result.run
    assert run.feature == A.FEATURE and run.user_id.ids == env.user.ids and run.res_id == budget.ids[0]
    sent = MockAdapter.captured[-1]["messages"][0]["content"]
    assert "planned 1,000.00" in sent and "actual 1,250.00" in sent


def test_scoping_isolation_and_kill_switch(env: Any) -> None:
    budget = _budget(env)
    _script(env, "Marketing is over plan.")
    seller = _user(env, "bud-ai-seller", ["sale.group_sale_user"], env.ref("core.main_company"))
    with pytest.raises(AccessError):
        A.explain(env.with_user(seller.ids[0]), env.with_user(seller.ids[0])["account.budget"].browse(budget.ids))
    eu = env.ref("core.demo_company_eu")
    eu_user = _user(env, "bud-ai-eu", ["account.group_account_manager"], eu)
    as_eu = env.with_user(eu_user.ids[0]).with_company(eu.ids[0])
    with pytest.raises(AccessError):
        A.explain(as_eu, as_eu["account.budget"].browse(budget.ids))
    assert MockAdapter.captured == []
    auditor = _user(env, "bud-ai-auditor", ["account.group_account_readonly"], env.ref("core.main_company"))
    as_auditor = env.with_user(auditor.ids[0])
    assert A.explain(as_auditor, as_auditor["account.budget"].browse(budget.ids)).text == "Marketing is over plan."
    with pytest.raises(UserError):
        A.prepare(env, None, {})
    calls = len(MockAdapter.captured)
    runs = env["ai_core.run"].search_count([("feature", "=", A.FEATURE)])
    env["ai_core.feature"].elevated().search([("name", "=", A.FEATURE)]).set_enabled(env.company.ids[0], False)
    with pytest.raises(FeatureDisabled):
        A.explain(env, budget)
    assert len(MockAdapter.captured) == calls
    assert env["ai_core.run"].search_count([("feature", "=", A.FEATURE)]) == runs


def test_api_flag(admin: Session) -> None:
    """Switched off, both the run endpoint (for a real budget) and the streaming one answer 404 feature_disabled."""
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
        budget = e["account.budget"].create(
            {"name": "Gate API budget", "date_from": D(2026, 1, 1), "date_to": D(2026, 12, 31)}
        )
        e.cr.commit()
        bid = budget.ids[0]
    flag = f"/api/v1/ai/admin/features/{A.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        for kind, res_id in (("run", bid), ("stream", 999999999)):
            off = admin.post(
                f"/api/v1/ai/features/{A.FEATURE}/{kind}", json={"res_model": "account.budget", "res_id": res_id}
            )
            assert off.status_code == 404 and off.json()["code"] == "feature_disabled", (kind, off.text)
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)
        with environment_for() as e:
            e["account.budget"].browse([bid]).unlink()
            e.cr.commit()


def test_evaluation_is_complete_and_passes_in_replay(env: Any) -> None:
    (spec,) = EV.feature_specs([A.FEATURE])
    assert EV.completeness([spec]) == []
    report = EV.evaluate(env, spec)
    assert report.passed, (report.failures, report.metrics)
    assert report.metrics["numeric_consistency"] == 1.0 and report.metrics["rubric_score"] >= 0.8
