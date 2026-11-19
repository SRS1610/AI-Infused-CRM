"""``account.cashflow_forecast``: the engine, the back-test generator, the ledger adapter, the numbers guard
of the narrative, the API, the kill switch and access."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account import ai_cashflow as C
from modules.account import ai_cashflow_synth as S
from tessera.ai import client as C_
from tessera.ai.errors import FeatureDisabled
from tessera.ai.runtime import get_feature, run_feature

D = dt.date


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C_.reset_inprocess_state()
    yield
    C_.set_client_factory(None)


def _data() -> C.CashData:
    today = D(2026, 6, 30)
    docs = [
        # Late Ltd pays 10 days after due, On Time SA on the due date
        C.Document("receivable", "Late Ltd", D(2026, 3, 1), D(2026, 3, 31), 1000.0, D(2026, 4, 10)),
        C.Document("receivable", "Late Ltd", D(2026, 4, 1), D(2026, 5, 1), 1000.0, D(2026, 5, 11)),
        C.Document("receivable", "On Time SA", D(2026, 3, 5), D(2026, 4, 4), 500.0, D(2026, 4, 4)),
        C.Document("receivable", "On Time SA", D(2026, 4, 5), D(2026, 5, 5), 500.0, D(2026, 5, 5)),
    ]
    flows = [C.Flow(D(2026, m, 28), -3000.0, "Salaries") for m in (3, 4, 5, 6)]
    flows += [C.Flow(D(2026, 6, 3), -120.0, "Card expense")]
    items = [
        C.OpenItem("receivable", "Late Ltd", D(2026, 7, 5), 2000.0, "INV/2026/00010"),
        C.OpenItem("receivable", "On Time SA", D(2026, 7, 6), 700.0, "INV/2026/00011"),
        C.OpenItem("payable", "Vendor", D(2026, 7, 9), 400.0, "BILL/2026/00003"),
        C.OpenItem("receivable", "Gone Ltd", D(2026, 1, 15), 800.0, "INV/2026/00001"),  # long overdue
    ]
    return C.CashData(today, 10000.0, items, docs, flows, "EUR")


def test_engine_shifts_open_items_by_partner_delay_and_projects_recurring() -> None:
    data = _data()
    delays, _by_kind = C.partner_delays(data.documents)
    assert delays[("receivable", "Late Ltd")] == 10 and delays[("receivable", "On Time SA")] == 0
    groups, _used = C.recurring_groups(data.flows, data.today)
    assert [(g["label"], g["amount"], g["day"]) for g in groups] == [("Salaries", -3000.0, 28)]
    result = C.forecast(data, 4)
    by_date = {d["date"]: d["net"] for d in result["daily"]}
    assert by_date["2026-07-15"] >= 1990.0  # Late Ltd: due 5 July + 10 days
    assert by_date["2026-07-06"] >= 690.0  # On Time SA on its due date
    assert by_date["2026-07-28"] <= -3000.0  # salaries
    overdue = next(b for b in result["largest_items"] if b["label"] == "INV/2026/00001")
    assert overdue["amount"] == 400.0 and overdue["date"] == "2026-07-07"  # half expected, next week
    assert (
        len(result["weeks"]) == 4
        and result["weeks"][0]["low"] <= result["weeks"][0]["position"] <= result["weeks"][0]["high"]
    )
    assert result["lowest"]["balance"] == min(d["balance"] for d in result["daily"])
    assert C.forecast(data, 99)["weeks"][-1]["week"] == C.MAX_WEEKS


def test_backtest_beats_a_flat_balance() -> None:
    errors, flat = [], []
    for seed in range(1, 31):
        params = S.scenario(seed)
        data, actual = S.simulate(params)
        assert len(actual) == params["weeks"] and data.today == S.START + dt.timedelta(days=S.HISTORY_DAYS)
        assert all(i.due and i.amount > 0 for i in data.open_items)
        pred = [w["position"] for w in C.forecast(data, params["weeks"])["weeks"]]
        errors.append(C.mape(pred, actual))
        flat.append(C.mape([data.opening] * len(actual), actual))
    assert sum(errors) / len(errors) < 0.2 and sum(errors) < sum(flat)
    assert S.simulate(S.scenario(5))[1] == S.simulate(S.scenario(5))[1]  # deterministic


def test_numbers_guard() -> None:
    facts = "Cash today: 12,450.00\nLowest position: 3,210.55 on 2026-07-28\nfor 8 weeks"
    assert C.foreign_numbers("Cash is 12,450.00 today, lowest 3,210.55 on 2026-07-28 within 8 weeks.", facts) == []
    assert C.foreign_numbers("About 12,450 today [source:core.company:7].", facts) == []
    assert C.foreign_numbers("Cash is 99,000.00 and grows 15%.", facts) == ["99,000.00", "15%"]
    hook = C.numbers_guard(facts, "fallback")
    data: dict[str, Any] = {"narrative": "It ends at 1.00"}
    hook(None, "", data)  # type: ignore[arg-type]
    assert data["narrative"] == "fallback" and data["narrative_source"] == "facts" and data["narrative_model"]
    data = {"narrative": "Cash today: 12,450.00."}
    hook(None, "", data)  # type: ignore[arg-type]
    assert data["narrative_source"] == "model"
    hook(None, "", None)  # type: ignore[arg-type]


@pytest.mark.db
def test_ledger_adapter_reads_balance_open_items_and_payments(env: Any) -> None:
    from modules.account.tests.common import invoice, partner

    eu = env.ref("core.demo_company_eu")
    env.user.elevated().write({"company_ids": [("add", eu.ids[0])]})
    e = env.with_company(eu.ids[0])
    today = e.today()
    customer = partner(e, "Cash Customer SAS")
    paid = invoice(
        e,
        customer,
        [{"name": "Service", "quantity": 1, "price_unit": 1000.0}],
        invoice_date=today - dt.timedelta(days=40),
    )
    paid.action_register_payment(date=(today - dt.timedelta(days=5)).isoformat())
    open_ = invoice(
        e,
        customer,
        [{"name": "Service", "quantity": 1, "price_unit": 500.0}],
        invoice_date=today - dt.timedelta(days=3),
    )
    data = C.cash_data(e, today)
    assert data.currency == "EUR"
    labels = [i.label for i in data.open_items]
    assert open_.display_name in labels and paid.display_name not in labels
    doc = next(d for d in data.documents if d.amount == pytest.approx(float(paid.amount_total)) and d.paid)
    assert doc.paid == today - dt.timedelta(days=5)
    assert data.opening >= float(paid.amount_total) - 0.01  # the payment reached the bank account
    result = C.forecast(data, 8)
    assert len(result["weeks"]) == 8 and result["open_receivables"] >= float(open_.amount_total) - 0.01
    assert "Cash today" in C.facts_text(result) and C.plain_summary(result)


@pytest.mark.db
def test_narrative_through_the_runtime_and_kill_switch(env: Any) -> None:
    result = C.forecast(_data(), 4)
    facts = C.facts_text(result)
    p = env.ref("ai_core.provider_local")
    good = f"Cash stands at {C._fmt(result['opening'])} today and ends at {C._fmt(result['closing'])}."
    p.write(
        {
            "options": {
                **(p.options or {}),
                "script": [{"match": {"feature": C.FEATURE}, "respond": {"json": {"narrative": good}}}],
            }
        }
    )
    run = run_feature(env, C.FEATURE, {"weeks": 4}, **C.narrative_kwargs(result, env.company.ids[0]))
    assert (
        run.data["narrative"] == good
        and run.data["narrative_source"] == "model"
        and C.foreign_numbers(good, facts) == []
    )
    assert run.run.feature == C.FEATURE and run.run.status == "ok"
    get_feature(env, C.FEATURE).set_enabled(env.company.ids[0], False)
    calls = len(MockAdapter.captured)
    with pytest.raises(FeatureDisabled):
        run_feature(env, C.FEATURE, {"weeks": 4}, **C.narrative_kwargs(result, env.company.ids[0]))
    with pytest.raises(FeatureDisabled):
        C.ensure_enabled(env)
    assert len(MockAdapter.captured) == calls


@pytest.mark.db
def test_api_forecast_narrative_and_access(admin: Any, app: Any) -> None:
    from fastapi.testclient import TestClient

    from tessera.api.tests.conftest import Session
    from tessera.orm.environment import environment_for

    got = admin.get("/api/v1/account/ai/cashflow", params={"weeks": 6})
    assert got.status_code == 200, got.text
    body = got.json()
    assert len(body["weeks"]) == 6 and body["summary"] and body["ai_generated"]
    told = admin.post("/api/v1/account/ai/cashflow/narrative", json={"weeks": 6})
    assert told.status_code == 200 and told.json()["forecast"]["weeks"][0]["week"] == 1
    assert told.json()["narrative_source"] in ("model", "facts") and not C.foreign_numbers(
        told.json()["narrative"], C.facts_text(told.json()["forecast"])
    )
    demo = Session(TestClient(app, base_url="http://localhost"), "demo", "demo1234")
    assert demo.get("/api/v1/account/ai/cashflow").status_code == 403
    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
    flag = f"/api/v1/ai/admin/features/{C.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        assert admin.get("/api/v1/account/ai/cashflow").json()["code"] == "feature_disabled"
        assert admin.post("/api/v1/account/ai/cashflow/narrative", json={}).status_code == 404
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)
