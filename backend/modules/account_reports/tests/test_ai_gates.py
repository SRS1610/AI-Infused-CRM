"""``account_reports.nl_report`` gates (2.6): kill switch → ``feature_disabled`` without gateway call or run,
permission scoping (report rights, names resolved only among records the user can read, companies only among the
user's), company isolation, the ``ai_core.run`` audit row, and nothing written — the answer is report parameters
for review, whose numbers come from the report engine."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account.tests.common import partner
from modules.account_reports import ai_features as A
from modules.account_reports.engine import run_report
from tessera.ai import client as C
from tessera.ai.errors import FeatureDisabled
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError

pytestmark = pytest.mark.db
TODAY = dt.date(2026, 11, 11)


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


BLANK = {"report": "", "period": "default", "date_from": None, "date_to": None, "comparison": "none", "periods": 1,
         "partners": [], "journals": [], "companies": [], "account_type": "all", "include_draft": False}  # fmt: skip


def _script(env: Any, reading: dict[str, Any]) -> None:
    reading = {**BLANK, **reading}
    p = env.ref("ai_core.provider_local")
    p.write(
        {"options": {**(p.options or {}), "script": [{"match": {"feature": A.FEATURE}, "respond": {"json": reading}}]}}
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


def test_question_becomes_parameters_for_review_and_writes_nothing(env: Any) -> None:
    acme = partner(env, "Gate Acme Corp")
    _script(env, {"report": "profit_loss", "period": "last_quarter", "comparison": "previous_year",
                  "partners": ["Gate Acme Corp"], "journals": [], "companies": []})  # fmt: skip
    moves = env["account.move"].search_count([])
    result = A.ask(env, "P&L last quarter vs last year for Gate Acme Corp", TODAY)
    params = result.data["params"]
    assert (
        params["report"] == "profit_loss" and params["date_from"] == "2026-07-01" and params["date_to"] == "2026-09-30"
    )
    assert params["comparison"] == "previous_year" and params["partner_ids"] == acme.ids
    assert result.to_dict()["ai_generated"] and not result.proposals
    assert env["account.move"].search_count([]) == moves
    run = result.run
    assert run.feature == A.FEATURE and run.user_id.ids == env.user.ids and run.status == "ok" and run.prompt_version
    # the figures come from the engine with these parameters, never from the model
    data = run_report(env, {k: v for k, v in params.items() if v not in (None, [])})
    assert data["report"]["code"] == "profit_loss"


def test_names_resolve_only_among_records_the_user_can_read(env: Any) -> None:
    eu = env.ref("core.demo_company_eu")
    eu_partner = (
        env["contacts.partner"]
        .elevated()
        .create({"name": "Gate Hidden GmbH", "is_company": True, "company_id": eu.ids[0]})
    )
    accountant = _user(env, "rep-ai-acc", ["account.group_account_readonly"], env.ref("core.main_company"))
    as_acc = env.with_user(accountant.ids[0])
    _script(env, {"report": "aged_receivable", "period": "today", "partners": ["Gate Hidden GmbH"],
                  "companies": ["My Company (Europe)"], "journals": []})  # fmt: skip
    result = A.ask(as_acc, "receivables of Gate Hidden GmbH in the European company", TODAY)
    data = result.data
    assert eu_partner.ids[0] not in (data["params"] or {}).get("partner_ids", [])
    assert data["unresolved"]["partners"] == ["Gate Hidden GmbH"]
    assert data["unresolved"]["companies"] == ["My Company (Europe)"]  # not one of the user's companies
    assert all("Gate Hidden" not in n for n in data.get("partner_names", []))


def test_report_rights_and_kill_switch(env: Any) -> None:
    seller = _user(env, "rep-ai-seller", ["sale.group_sale_user"], env.ref("core.main_company"))
    _script(env, {"report": "profit_loss", "period": "ytd"})
    with pytest.raises(AccessError):
        A.ask(env.with_user(seller.ids[0]), "P&L this year", TODAY)
    assert MockAdapter.captured == []
    runs = env["ai_core.run"].search_count([("feature", "=", A.FEATURE)])
    env["ai_core.feature"].elevated().search([("name", "=", A.FEATURE)]).set_enabled(env.company.ids[0], False)
    with pytest.raises(FeatureDisabled):
        A.ask(env, "P&L this year", TODAY)
    assert env["ai_core.run"].search_count([("feature", "=", A.FEATURE)]) == runs and MockAdapter.captured == []


def test_api_flag(admin: Session) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
    url = f"/api/v1/ai/features/{A.FEATURE}/run"
    body = {"inputs": {"question": "trial balance today"}}
    flag = f"/api/v1/ai/admin/features/{A.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        off = admin.post(url, json=body)
        assert off.status_code == 404 and off.json()["code"] == "feature_disabled", off.text
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


def test_evaluation_is_complete_and_passes_in_replay(env: Any) -> None:
    from tessera.ai import evals as EV

    (spec,) = EV.feature_specs([A.FEATURE])
    assert EV.completeness([spec]) == []
    report = EV.evaluate(env, spec)
    assert report.passed, (report.failures, report.metrics)
