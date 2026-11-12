"""``account_dunning.message_draft`` gates (2.6): the draft is a write proposal on the prepared reminder — nothing
changes and no mail is queued before acceptance, and accepting only replaces the text (sending stays the run's
action); figures not in the ledger are removed; kill switch → ``feature_disabled`` without gateway call or run;
permission scoping and company isolation; the ``ai_core.run`` audit row."""

from __future__ import annotations

from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account_dunning import ai_features as A
from modules.account_dunning.ai_eval import build
from tessera.ai import client as C
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


def _script(env: Any, subject: str, body: str) -> None:
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": [
        {"match": {"feature": A.FEATURE}, "respond": {"json": {"subject": subject, "body": body}}}]}})  # fmt: skip


def _reminder(env: Any) -> Any:
    customer = env["contacts.partner"].create(
        {"name": "Gate Robotics Ltd", "is_company": True, "email": "ap@gate.test"}
    )
    return build(
        env, {"day": "2026-06-30", "partner": customer.ids[0], "invoices": [{"amount": 1840.5, "days_late": 21}]}
    )


def _mail_count(env: Any) -> int:
    env.flush()
    return int(env.cr.scalar("SELECT count(*) FROM core_mail_queue") or 0)


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


def test_draft_is_a_proposal_nothing_sent_and_figures_checked(env: Any) -> None:
    line = _reminder(env)
    before = (line.subject, line.message, line.state)
    mails = _mail_count(env)
    _script(
        env,
        "Reminder: 1,840.50 overdue",
        "Dear Gate Robotics, invoice of 1,840.50 is 21 days late. Pay within 3 days to keep a 10% discount.",
    )
    result = A.draft(env, line)
    data = result.data
    assert data["removed_numbers"] == ["10"] and "discount" not in data["body"]  # the whole sentence goes
    assert "1,840.50" in data["body"] and data["tone"] in ("friendly", "firm", "formal")
    (proposal,) = result.proposals
    assert proposal.kind == "write" and proposal.state == "pending" and proposal.res_id == line.ids[0]
    assert (line.subject, line.message, line.state) == before and _mail_count(env) == mails  # nothing before
    run = result.run
    assert run.feature == A.FEATURE and run.user_id.ids == env.user.ids and run.res_id == line.ids[0]
    assert result.to_dict()["ai_generated"]
    P.accept(env, env["ai_core.proposal"].browse(proposal.ids))
    env.cache.invalidate()
    assert line.subject == "Reminder: 1,840.50 overdue" and "21 days late" in line.message
    assert line.state == before[2] and _mail_count(env) == mails  # sending stays the run's own action
    assert "1,840.50" in line.message  # the ledger's invoice table is appended


def test_scoping_isolation_state_and_kill_switch(env: Any) -> None:
    line = _reminder(env)
    _script(env, "Reminder", "Please pay the amount due.")
    seller = _user(env, "dun-ai-seller", ["sale.group_sale_user"], env.ref("core.main_company"))
    with pytest.raises(AccessError):
        A.draft(env.with_user(seller.ids[0]), env.with_user(seller.ids[0])["account.dunning_run_line"].browse(line.ids))
    eu = env.ref("core.demo_company_eu")
    eu_user = _user(env, "dun-ai-eu", ["account.group_account_manager"], eu)
    as_eu = env.with_user(eu_user.ids[0]).with_company(eu.ids[0])
    with pytest.raises(AccessError):
        A.draft(as_eu, as_eu["account.dunning_run_line"].browse(line.ids))
    assert MockAdapter.captured == []
    auditor = _user(env, "dun-ai-auditor", ["account.group_account_readonly"], env.ref("core.main_company"))
    as_auditor = env.with_user(auditor.ids[0])
    result = A.draft(as_auditor, as_auditor["account.dunning_run_line"].browse(line.ids))
    assert result.data["body"] and not result.proposals  # read-only: a draft, no proposal
    line.elevated().write({"state": "sent"})
    with pytest.raises(UserError):
        A.draft(env, line)
    with pytest.raises(UserError):
        A.readable_line(env, None)
    line.elevated().write({"state": "draft"})
    calls = len(MockAdapter.captured)
    runs = env["ai_core.run"].search_count([("feature", "=", A.FEATURE)])
    env["ai_core.feature"].elevated().search([("name", "=", A.FEATURE)]).set_enabled(env.company.ids[0], False)
    with pytest.raises(FeatureDisabled):
        A.draft(env, line)
    assert len(MockAdapter.captured) == calls
    assert env["ai_core.run"].search_count([("feature", "=", A.FEATURE)]) == runs


def test_api_flag(admin: Session) -> None:
    """The streaming endpoint checks the switch before anything else (the run endpoint after loading the record)."""
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
    url = f"/api/v1/ai/features/{A.FEATURE}/stream"
    flag = f"/api/v1/ai/admin/features/{A.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        off = admin.post(url, json={"res_model": "account.dunning_run_line", "res_id": 999999999})
        assert off.status_code == 404 and off.json()["code"] == "feature_disabled", off.text
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


def test_evaluation_is_complete_and_passes_in_replay(env: Any) -> None:
    from tessera.ai import evals as EV

    (spec,) = EV.feature_specs([A.FEATURE])
    assert EV.completeness([spec]) == []
    report = EV.evaluate(env, spec)
    assert report.passed, (report.failures, report.metrics)
