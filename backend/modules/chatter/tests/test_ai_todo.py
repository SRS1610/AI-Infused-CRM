"""AI-ranked daily to-do (BUILD_PLAN 1.6): rules, reasons, permissions, API, morning job, latency, eval."""

from __future__ import annotations

import datetime as dt
import time
import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway.adapters.mock import MockAdapter

from modules.chatter import ai_todo as T
from tessera.ai import client as C
from tessera.ai import evals as E
from tessera.ai.errors import FeatureDisabled
from tessera.api.tests.conftest import Session

pytestmark = pytest.mark.db

TODAY = dt.date(2026, 11, 10)


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def _lead(env: Any, name: str, value: float, prob: float, stars: int = 0, company: str = "core.main_company") -> Any:
    return (
        env["crm.lead"]
        .elevated()
        .create(
            {
                "name": name,
                "type": "opportunity",
                "expected_revenue": value,
                "probability": prob,
                "priority": str(stars),
                "company_id": env.ref(company).ids[0],
            }
        )
    )


def _activity(env: Any, user: Any, record: Any, kind: str, summary: str, days: int, today: dt.date = TODAY) -> Any:
    return (
        env["chatter.activity"]
        .elevated()
        .create(
            {
                "model": record._name,
                "res_id": record.ids[0],
                "res_name": record.elevated().display_name,
                "activity_type_id": env.ref(f"chatter.activity_type_{kind}").ids[0],
                "summary": summary,
                "date_deadline": (today + dt.timedelta(days=days)).isoformat(),
                "user_id": user.uid,
                "company_id": user.company.ids[0],
            }
        )
    )


def _user(env: Any, company: str = "core.main_company") -> Any:
    group = env.ref("core.group_user").ids[0]
    cid = env.ref(company).ids[0]
    u = env["core.user"].elevated().create(
        {"login": f"todo-{uuid.uuid4().hex[:6]}", "name": "Tess Todo", "company_id": cid, "company_ids": [("set", [cid])],
         "group_ids": [("set", [group, env.ref("crm.group_crm_user").ids[0]])]}
    )  # fmt: skip
    return env.with_user(u.ids[0]).with_company(cid)


def test_rules_rank_with_reasons_and_without_a_gateway_call(env: Any) -> None:
    me = _user(env)
    big = _lead(env, "Globex expansion", 200000, 50, stars=2)
    small = _lead(env, "Beta add-on", 5000, 20)
    a = _activity(env, me, big, "call", "Negotiate Globex", 0)
    b = _activity(env, me, small, "todo", "Update notes", -4)
    c = _activity(env, me, small, "email", "Send brochure", 3)
    other = _user(env)
    _activity(env, other, big, "call", "Someone else's call", -10)  # never in my list
    items = T.rank(me, TODAY)
    assert [i["activity_id"] for i in items] == [a.ids[0], b.ids[0], c.ids[0]]
    assert items[0]["reasons"] == [
        "Due today",
        "Call with other people",
        "Globex expansion is worth 200,000 USD at 50% probability",
        "Priority ★★",
    ]
    assert items[1]["reasons"][0] == "Overdue by 4 days" and items[2]["reasons"][0] == "Due in 3 days"
    assert [i["rank"] for i in items] == [1, 2, 3]
    assert MockAdapter.captured == []  # rules only: nothing leaves the deployment


def test_a_linked_record_the_user_cannot_read_contributes_nothing(env: Any) -> None:
    marie = _user(env, "core.demo_company_eu")
    hidden = _lead(env, "HQ acquisition", 987654, 90, stars=3)  # main company: not readable by an EU-only user
    mine = _lead(env, "Lyon prospect", 30000, 50, company="core.demo_company_eu")
    h = _activity(env, marie, hidden, "todo", "Prepare board deck", -1)
    m = _activity(env, marie, mine, "call", "Call Lyon", 0)
    items = {i["activity_id"]: i for i in T.rank(marie, TODAY)}
    assert items[h.ids[0]]["reasons"] == ["Overdue by 1 day", "Linked record not visible to you"]
    assert "987" not in str(items) and "★★★" not in str(items[h.ids[0]])
    currency = mine.elevated().currency_id.name
    assert f"Lyon prospect is worth 30,000 {currency} at 50% probability" in items[m.ids[0]]["reasons"]


def test_on_demand_run_is_audited_and_respects_the_kill_switch(env: Any) -> None:
    me = _user(env)
    _activity(env, me, _lead(env, "Initech renewal", 45000, 80), "call", "Renewal call", 0, today=me.today())
    before = int(env.cr.scalar("SELECT count(*) FROM chatter_activity") or 0)
    out = T.daily_todo(me)
    run = env["ai_core.run"].elevated().browse([out["run_id"]])
    assert run.feature == "chatter.daily_todo" and run.provider == "rules" and run.status == "ok"
    assert run.prompt_version == "chatter.daily_todo@1" and run.user_id.ids == [me.uid] and run.cost == 0
    assert run.output_data["items"][0]["summary"] == "Renewal call" and out["deterministic"] is True
    assert int(env.cr.scalar("SELECT count(*) FROM chatter_activity") or 0) == before  # nothing changed
    env["ai_core.feature"].search([("name", "=", T.FEATURE)]).set_enabled(me.company.ids[0], False)
    with pytest.raises(FeatureDisabled):
        T.daily_todo(me)


def test_morning_job_notifies_each_user_with_a_link(env: Any, system_env: Any) -> None:
    me = _user(env)
    idle = _user(env)
    today = me.today()
    _activity(env, me, _lead(env, "Wayne renewal", 75000, 85), "meeting", "Renewal meeting", 0, today=today)
    _activity(env, me, _lead(env, "Stark upsell", 10000, 20), "email", "Send agenda", 1, today=today)
    _activity(env, idle, _lead(env, "Far away", 1000, 10), "todo", "Someday", 30, today=today)
    out = T.morning_job(system_env)
    assert out["users"] >= 1
    notes = env["core.notification"].elevated().search([("user_id", "=", me.uid)])
    (note,) = notes
    assert note.title == "Your AI-ranked to-do for today: 2 activities" and note.kind == "activity"
    assert note.url == T.TODO_URL and note.body.startswith(
        "Start with meeting: Renewal meeting (Due today; Meeting today"
    )
    assert not env["core.notification"].elevated().search([("user_id", "=", idle.uid)])  # nothing due soon
    job = env.ref("chatter.sched_ai_daily_todo")
    assert job.job_name == "chatter.ai_daily_todo"


def test_interactive_p95_under_3_seconds(env: Any) -> None:
    me = _user(env)
    leads = [_lead(env, f"Deal {i}", 1000 * (i + 1), 10 + i) for i in range(40)]
    for i, lead in enumerate(leads):
        _activity(env, me, lead, ["call", "email", "todo", "meeting"][i % 4], f"Step {i}", (i % 15) - 5)
    durations = []
    for _ in range(20):
        started = time.monotonic()
        assert len(T.daily_todo(me)["items"]) == 40
        durations.append(time.monotonic() - started)
    assert sorted(durations)[18] < 3.0


def test_spearman() -> None:
    assert T.spearman([1, 2, 3, 4], [1, 2, 3, 4]) == 1.0
    assert T.spearman([4, 3, 2, 1], [1, 2, 3, 4]) == -1.0
    assert T.spearman([2, 1, 3], [1, 2, 3]) == pytest.approx(0.5)
    assert T.spearman([9, 1, 2, 3], [1, 2, 3]) == 1.0  # items outside the labelled set are ignored
    scores = T.spearman_scorer(
        [{"status": "ok", "data": {"items": [{"activity_id": 2}, {"activity_id": 1}, {"activity_id": 3}]},
          "_case": {"expected": {"order": [1, 2, 3]}}}]
    )  # fmt: skip
    assert scores == {"rank_correlation": pytest.approx(0.5), "top1_agreement": 0.0}


def test_eval_set_meets_the_rank_correlation_threshold(env: Any) -> None:
    (spec,) = E.feature_specs(["chatter.daily_todo"])
    assert E.completeness([spec]) == []
    report = E.evaluate(env, spec)
    assert report.passed, (report.failures, report.metrics)
    assert report.metrics["rank_correlation"] >= 0.6 and report.cases >= 30


def test_api(admin: Session, app: Any) -> None:
    demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    resp = demo.get("/api/v1/chatter/ai/todo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["deterministic"] is True and isinstance(body["items"], list) and body["run_id"]
    assert demo.post(f"/api/v1/ai/runs/{body['run_id']}/feedback", json={"rating": "up"}).status_code == 201
    assert "chatter.daily_todo" in demo.get("/api/v1/ai/status").json()["features"]
