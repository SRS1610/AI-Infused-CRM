"""``account_bank.reconcile_suggest`` gates (2.6): kill switch → ``feature_disabled`` without gateway call or run,
permission scoping and company isolation, the ``ai_core.run`` audit row, and proposal-only matching — nothing is
reconciled before a person accepts the proposal; the model only breaks ties among offered candidates."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account.tests.common import eu_env, invoice, partner
from modules.account_bank import ai_features as A
from modules.account_bank.tests.common import bank_journal, statement
from tessera.ai import client as C
from tessera.ai import proposals as P
from tessera.ai.errors import FeatureDisabled
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError, MissingError

pytestmark = pytest.mark.db
D = dt.date


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def _user(env: Any, login: str, groups: list[str], company: Any | None = None) -> Any:
    company = company or env.company
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


def _case(env: Any, label: str = "VIR NORDWIND GMBH RECHNUNG 2026 7") -> tuple[Any, Any]:
    customer = partner(env, "Nordwind GmbH")
    inv = invoice(env, customer, [{"price_unit": 840.0}], invoice_date=D(2026, 3, 2))
    line = statement(env, [{"amount": 840.0, "payment_ref": label, "date": D(2026, 3, 20)}]).line_ids
    return inv, line


def test_suggestion_is_a_proposal_and_nothing_is_matched_before_acceptance(env: Any) -> None:
    inv, line = _case(env)
    out = A.suggest(env, line)
    best = out["suggestions"][0]
    assert inv.ids[0] in {i["move_id"] for i in best["items"]}
    assert out["proposal"]["state"] == "pending" and out["proposal"]["kind"] == "action" and out["ai_generated"]
    assert line.state == "posted" and inv.payment_state == "not_paid"  # nothing before acceptance
    run = env["ai_core.run"].browse([out["run_id"]])
    assert run.feature == A.FEATURE and run.user_id.ids == env.user.ids and run.company_id == env.company
    assert run.res_model == "account.bank_statement_line" and run.res_id == line.ids[0] and run.decision == "pending"
    P.accept(env, env["ai_core.proposal"].browse([out["proposal"]["id"]]))
    env.cache.invalidate()
    assert line.state == "reconciled" and inv.payment_state in ("paid", "in_payment")
    assert run.decision == "accepted"
    with pytest.raises(MissingError):
        A.apply_match(env, "account.bank_statement_line", 999999999, {"items": []})


def test_rejected_proposal_changes_nothing_and_many_lines_get_one_proposal_each(env: Any) -> None:
    inv, line = _case(env)
    out = A.suggest(env, line)
    P.reject(env, env["ai_core.proposal"].browse([out["proposal"]["id"]]), "not this one")
    env.cache.invalidate()
    assert line.state == "posted" and inv.payment_state == "not_paid"
    _inv2, line2 = _case(env, "NORDWIND RECHNUNG")
    many = A.suggest_many(env, line | line2)
    assert len(many["rows"]) == 2 and many["ai_generated"]
    assert all(r["run_id"] for r in many["rows"])


def test_permission_scoping_and_company_isolation(env: Any) -> None:
    _inv, line = _case(env)
    clerk = env.with_user(_user(env, "bank-ai-billing", ["account.group_account_invoice"]).ids[0])
    with pytest.raises(AccessError):
        A.suggest(clerk, clerk["account.bank_statement_line"].browse(line.ids))
    auditor = env.with_user(_user(env, "bank-ai-auditor", ["account.group_account_readonly"]).ids[0])
    out = A.suggest(auditor, auditor["account.bank_statement_line"].browse(line.ids))
    assert out["suggestions"] and out["proposal"] is None  # read-only: suggestions, no proposal
    e = eu_env(env)
    eu_line = statement(e, [{"amount": 5.0, "payment_ref": "EU only"}], journal=bank_journal(e)).line_ids
    us = env.with_user(_user(env, "bank-ai-us", ["account.group_account_user"], env.ref("core.main_company")).ids[0])
    with pytest.raises(AccessError):
        A.suggest(us, us["account.bank_statement_line"].browse(eu_line.ids))
    # a US user's candidates never include another company's items
    out = A.suggest(us, us["account.bank_statement_line"].browse(line.ids))
    eu_moves = set(e["account.move"].search([("company_id", "=", e.company.ids[0])]).ids)
    assert not {i["move_id"] for s in out["suggestions"] for i in s["items"]} & eu_moves


def test_kill_switch_makes_no_run_and_no_gateway_call(env: Any) -> None:
    _inv, line = _case(env)
    runs = env["ai_core.run"].search_count([("feature", "=", A.FEATURE)])
    env["ai_core.feature"].elevated().search([("name", "=", A.FEATURE)]).set_enabled(env.company.ids[0], False)
    for fn in (lambda: A.suggest(env, line), lambda: A.suggest_many(env, line)):
        with pytest.raises(FeatureDisabled):
            fn()
    assert env["ai_core.run"].search_count([("feature", "=", A.FEATURE)]) == runs and MockAdapter.captured == []
    assert env["ai_core.proposal"].search_count([("feature", "=", A.FEATURE), ("res_id", "=", line.ids[0])]) == 0


def test_api_routes_and_flag(admin: Session) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
    assert admin.get("/api/v1/account_bank/ai/enabled").json() == {"enabled": True}
    assert admin.post("/api/v1/account_bank/ai/lines/999999999/suggest").status_code == 404
    ok = admin.post("/api/v1/account_bank/ai/suggest", json={"line_ids": []})
    assert ok.status_code == 200 and ok.json()["ai_generated"]
    flag = f"/api/v1/ai/admin/features/{A.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        for url, body in (
            ("/api/v1/account_bank/ai/lines/999999999/suggest", None),
            ("/api/v1/account_bank/ai/suggest", {}),
        ):
            off = admin.post(url, json=body) if body is not None else admin.post(url)
            assert off.status_code == 404 and off.json()["code"] == "feature_disabled", (url, off.text)
        assert admin.get("/api/v1/account_bank/ai/enabled").json() == {"enabled": False}
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)
