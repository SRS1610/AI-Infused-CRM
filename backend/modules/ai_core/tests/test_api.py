"""AI HTTP API: status, runs, streaming, proposals, kill switch, degradation, cost dashboard."""

from __future__ import annotations

import json
import time
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from gateway.adapters.mock import MockAdapter

from modules.ai_core import api as ai_api
from tessera.ai import client as C
from tessera.api.tests.conftest import Session

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    ai_api._health_cache.update(at=0.0, ok=False)
    yield
    C.set_client_factory(None)
    ai_api._health_cache.update(at=0.0, ok=False)


def _ticket(admin: Session) -> int:
    resp = admin.post(
        "/api/v1/models/ai_test.ticket",
        json={
            "values": {
                "name": f"API ticket {uuid.uuid4().hex[:6]}",
                "description": "Toner smears",
                "customer_name": "Paul Durand",
            }
        },
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


def _sse(resp: Any) -> list[dict[str, Any]]:
    events = []
    for line in resp.iter_lines():
        if line.startswith("data:"):
            events.append({**json.loads(line[5:]), "_at": time.monotonic()})
    return events


def test_status_and_blocking_run(admin: Session) -> None:
    status = admin.get("/api/v1/ai/status").json()
    assert status["available"] is True and "ai_test.classify" in status["features"]
    resp = admin.post("/api/v1/ai/features/ai_test.classify/run", json={"inputs": {"text": "Outage"}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ai_generated"] and body["data"]["label"] == "urgent" and body["status"] == "ok"
    run = admin.get(f"/api/v1/ai/runs/{body['run_id']}").json()
    assert run["prompt_version"] == "ai_test.classify@1" and run["cost"] > 0
    assert (
        admin.post(f"/api/v1/ai/runs/{body['run_id']}/feedback", json={"rating": "up", "comment": "good"}).status_code
        == 201
    )
    assert admin.post("/api/v1/ai/features/ai_test.classify/run", json={"inputs": {"bogus": 1}}).status_code == 422


def test_kill_switch_hides_and_404s_for_one_company(admin: Session, app: Any) -> None:
    """1.5-j through the API: hidden in status, 404 feature_disabled, no gateway call, others unaffected."""
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]  # the administrator's current company
    r = admin.post("/api/v1/ai/features/ai_test.classify/run", json={"inputs": {"text": "warm-up"}})
    assert r.status_code == 200
    MockAdapter.reset()
    put = admin.client.put(
        "/api/v1/ai/admin/features/ai_test.classify/flag",
        json={"company_id": company, "enabled": False},
        headers=admin.headers,
    )
    assert put.status_code == 200 and put.json()["enabled"] is False
    try:
        assert "ai_test.classify" not in admin.get("/api/v1/ai/status").json()["features"]
        resp = admin.post("/api/v1/ai/features/ai_test.classify/run", json={"inputs": {"text": "x"}})
        assert resp.status_code == 404 and resp.json()["code"] == "feature_disabled"
        stream = admin.post("/api/v1/ai/features/ai_test.classify/stream", json={"inputs": {"text": "x"}})
        assert stream.status_code == 404
        assert MockAdapter.captured == []
        flags = {f["name"]: f for f in admin.get("/api/v1/ai/admin/features").json()}
        assert flags["ai_test.classify"]["enabled_global"] is True
    finally:
        admin.client.put(
            "/api/v1/ai/admin/features/ai_test.classify/flag",
            json={"company_id": company, "enabled": True},
            headers=admin.headers,
        )


def test_stream_draft_label_and_proposal_accept(admin: Session) -> None:
    """1.5-l / 1.5-i through the API: first token fast, AI-drafted until accepted, accept posts the message."""
    tid = _ticket(admin)
    started = time.monotonic()
    with admin.client.stream(
        "POST",
        "/api/v1/ai/features/ai_test.draft/stream",
        json={"res_model": "ai_test.ticket", "res_id": tid},
        headers=admin.headers,
    ) as resp:
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
        events = _sse(resp)
    kinds = [e["event"] for e in events]
    assert kinds[0] == "start" and kinds[-1] == "done" and "delta" in kinds
    first_delta = next(e for e in events if e["event"] == "delta")
    assert first_delta["_at"] - started < 1.5
    result = events[-1]["result"]
    text = "".join(e["text"] for e in events if e["event"] == "delta")
    assert result["text"] == text and result["ai_generated"]
    (proposal,) = result["proposals"]
    assert proposal["kind"] == "message" and proposal["ai_generated"] is True and proposal["state"] == "pending"
    pending = admin.get("/api/v1/ai/proposals", params={"res_model": "ai_test.ticket", "res_id": tid}).json()
    assert [p["id"] for p in pending] == [proposal["id"]]
    edited = {"body": "Hello Paul, we are on it."}
    accepted = admin.post(f"/api/v1/ai/proposals/{proposal['id']}/accept", json={"payload": edited}).json()
    assert accepted["state"] == "edited" and accepted["ai_generated"] is False
    msgs = admin.get(f"/api/v1/chatter/ai_test.ticket/{tid}/messages").json()["items"]
    assert msgs[0]["body"].startswith("<p>Hello Paul") or "Hello Paul" in msgs[0]["body"]
    assert msgs[0]["ai_assisted"] is True
    runs = admin.get("/api/v1/ai/runs", params={"res_model": "ai_test.ticket", "res_id": tid}).json()
    assert runs[0]["decision"] == "edited"


def test_proposals_belong_to_their_requester(admin: Session, app: Any) -> None:
    tid = _ticket(admin)
    res = admin.post(
        "/api/v1/ai/features/ai_test.draft/run", json={"res_model": "ai_test.ticket", "res_id": tid}
    ).json()
    pid = res["proposals"][0]["id"]
    demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    assert demo.post(f"/api/v1/ai/proposals/{pid}/accept", json={}).status_code == 403
    assert demo.get("/api/v1/ai/admin/usage").status_code == 403
    rejected = admin.post(f"/api/v1/ai/proposals/{pid}/reject", json={"reason": "wrong tone"}).json()
    assert rejected["state"] == "rejected"
    assert admin.post(f"/api/v1/ai/proposals/{pid}/accept", json={}).status_code == 409


def test_gateway_down_shows_unavailable_and_manual_path_works(admin: Session) -> None:
    """1.5-m: AI reports unavailable, its endpoints return 503, nothing else is affected."""
    C.set_client_factory(lambda e: C.HttpGatewayClient("http://127.0.0.1:9", "token", timeout=0.5))
    assert admin.get("/api/v1/ai/status").json()["available"] is False
    resp = admin.post("/api/v1/ai/features/ai_test.classify/run", json={"inputs": {"text": "x"}})
    assert resp.status_code == 503 and resp.json()["code"] == "ai_unavailable" and resp.json()["run_id"]
    tid = _ticket(admin)
    assert admin.patch(f"/api/v1/models/ai_test.ticket/{tid}", json={"values": {"state": "open"}}).status_code == 200
    assert admin.get("/api/v1/ai/admin/usage").json()["available"] is False


def test_cost_dashboard_matches_ledger_to_the_cent(admin: Session) -> None:
    """1.5-n: per-company, per-feature tokens, spend, latency percentiles and acceptance rate."""
    for i in range(3):
        admin.post("/api/v1/ai/features/ai_test.classify/run", json={"inputs": {"text": f"dashboard {i}"}})
    tid = _ticket(admin)
    res = admin.post(
        "/api/v1/ai/features/ai_test.draft/run", json={"res_model": "ai_test.ticket", "res_id": tid}
    ).json()
    admin.post(f"/api/v1/ai/proposals/{res['proposals'][0]['id']}/accept", json={})
    data = admin.get("/api/v1/ai/admin/usage").json()
    assert data["available"]
    rows = {(r["company_id"], r["feature"]): r for r in data["rows"]}
    from tessera.config import get_settings
    from tessera.db.engine import get_engine

    with get_engine(get_settings().database_url).connect() as conn:
        ledger = conn.exec_driver_sql(
            "SELECT company_id, feature, sum(cost), count(*), sum(output_tokens) FROM ai_gateway.ledger "
            "WHERE status IN ('ok','refusal','invalid_output') AND ts >= date_trunc('month', now()) GROUP BY 1, 2"
        ).all()
    for company_id, feature, cost, count, out_tokens in ledger:
        row = rows[(company_id, feature)]
        assert Decimal(row["cost"]) == Decimal(cost) and row["requests"] == count and row["output_tokens"] == out_tokens
    draft = next(r for (c, f), r in rows.items() if f == "ai_test.draft")
    assert draft["acceptance_rate"] is not None and draft["feature_title"] == "Draft a reply"
    assert draft["latency_p95"] >= draft["latency_p50"]


def test_run_content_only_for_its_owner(admin: Session, app: Any) -> None:
    """Someone who can read the record sees the audit skeleton of another user's run, never its output."""
    tid = _ticket(admin)
    run_id = admin.post(
        "/api/v1/ai/features/ai_test.draft/run", json={"res_model": "ai_test.ticket", "res_id": tid}
    ).json()["run_id"]
    demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    seen = demo.get(f"/api/v1/ai/runs/{run_id}").json()
    assert seen["feature"] == "ai_test.draft" and "output_text" not in seen and "proposals" not in seen
    assert "output_text" in admin.get(f"/api/v1/ai/runs/{run_id}").json()
