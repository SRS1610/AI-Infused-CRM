"""Rental AI features (2.6): demand forecast and usage alerts.

Covers the statistics (profile, robust trend, range, short histories), the forecast of a real product from its
reservations (what is already booked, other companies' rentals left out), the narrative's grounding, the usage check
on real rentals (late returns, long bookings, utilisation shifts), alerts and notifications without duplicates,
dismissing, the job and the kill switch, the API (404 ``feature_disabled``, access), and the evaluation gate.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account.tests.common import eu_env
from modules.sale.tests.common import customer
from modules.sale_rental import ai_demand as ML
from modules.sale_rental import ai_features as A
from modules.sale_rental.tests.common import rental_line, rental_order, rental_product
from tessera.ai import client as C
from tessera.ai import evals as EV
from tessera.ai.errors import FeatureDisabled
from tessera.ai.runtime import HANDLERS, run_feature
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db
UTC = dt.UTC
NOW = dt.datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def _days(n: float) -> dt.timedelta:
    return dt.timedelta(days=n)


# -- statistics ------------------------------------------------------------------------------------------------


def test_forecast_statistics() -> None:
    first = dt.date(2026, 1, 5)  # a Monday
    weekend = [2, 2, 2, 3, 6, 8, 7] * 20
    fc = ML.forecast(weekend, first, weeks=3, units=8)
    assert fc["method"].startswith("weekly profile") and len(fc["days"]) == 21 and len(fc["weeks"]) == 3
    assert fc["weekday_factors"][5] > 1.5 > fc["weekday_factors"][0]  # Saturdays are busy
    assert fc["weeks"][0]["unit_days"] == pytest.approx(30, abs=0.5) and fc["trend_per_week"] == 0
    assert fc["weeks"][0]["low"] <= fc["weeks"][0]["unit_days"] <= fc["weeks"][0]["high"]
    assert fc["weeks"][0]["utilisation"] == pytest.approx(30 / 56, abs=0.01)
    # a growing series keeps growing, damped; a one-off spike does not move the robust trend much
    growing = [round(2 + 0.3 * (i // 7)) for i in range(140)]
    spiky = list(growing)
    spiky[100] = 40
    a, b = ML.forecast(growing, first, weeks=1), ML.forecast(spiky, first, weeks=1)
    assert a["trend_per_week"] > 0 and abs(a["weeks"][0]["unit_days"] - b["weeks"][0]["unit_days"]) < 3
    # capped at the units, never negative
    assert all(d["units_out"] <= 3 for d in ML.forecast([9] * 60, first, units=3)["days"])
    assert all(d["units_out"] >= 0 for d in ML.forecast([5 - i // 7 for i in range(70)], first)["days"])
    # short or empty histories fall back to their average
    assert ML.forecast([1, 3], first, weeks=1)["method"] == "average (short history)"
    assert ML.forecast([], first, weeks=1)["weeks"][0]["unit_days"] == 0
    assert ML.theil_sen([]) == (0.0, 0.0) and ML.theil_sen([4]) == (0.0, 4.0)
    assert ML.weekly_profile([0] * 30, 0) == [1.0] * 7
    assert ML.mape([], []) != ML.mape([], [])  # nan
    rng = random.Random(1)
    s = ML.synthetic_demand(rng)
    assert len(s["history"]) == 182 and len(s["actual_weeks"]) == 4


def test_detectors() -> None:
    def rental(line: int, partner: int, start: float, days: float, late_h: float, product: int = 1) -> dict[str, Any]:
        s = NOW - _days(start)
        return {"line_id": line, "order_id": line, "partner_id": partner, "partner": f"P{partner}", "product_id": product,
                "start": s, "planned_end": s + _days(days), "returned": s + _days(days) + dt.timedelta(hours=late_h),
                "status": "returned"}  # fmt: skip

    rentals = [rental(i, 1, 100 - 10 * i, 2, 6 if i % 2 else 0) for i in range(5)]  # late 2 of 5
    rentals += [rental(10 + i, 2, 100 - 10 * i, 2, 5 if i == 0 else 0) for i in range(5)]  # late once
    flags = ML.late_return_flags(rentals, NOW)
    assert [f["partner_id"] for f in flags] == [1] and flags[0]["details"] == {
        "late": 2,
        "rentals": 5,
        "max_hours_late": 6,
    }
    overdue = {**rental(30, 3, 5, 2, 0), "returned": None, "status": "late"}
    early = {**rental(31, 3, 8, 2, 0)}
    later = {**rental(32, 3, 20, 1, 30)}
    assert [f["partner_id"] for f in ML.late_return_flags([overdue, early, later], NOW)] == [3]
    # long bookings against the product's usual durations
    usual = [rental(100 + i, 5, 150 - 5 * i, 2 + (i % 3) * 0.2, 0, product=9) for i in range(10)]
    long = rental(200, 6, 10, 14, 0, product=9)
    flags = ML.long_booking_flags([*usual, long], NOW - _days(30))
    assert [f["line_id"] for f in flags] == [200] and flags[0]["details"]["days"] == 14.0
    assert not ML.long_booking_flags([*usual[:4], long], None)  # too few bookings to compare with
    # utilisation: a doubled last week is a spike, a collapsed one a drop
    base = [3, 3, 3, 3, 5, 6, 5] * 10
    spike = base[:-7] + [v * 2 for v in base[-7:]]
    drop = base[:-7] + [0] * 7
    day = dt.date(2026, 11, 2)
    assert [f["kind"] for f in ML.utilisation_flags({1: {"units": 12, "history": spike}}, day)] == ["utilisation_spike"]
    assert [f["kind"] for f in ML.utilisation_flags({1: {"units": 12, "history": drop}}, day)] == ["utilisation_drop"]
    assert not ML.utilisation_flags({1: {"units": 12, "history": base}, 2: {"history": base[:20]}}, day)


# -- the forecast of a real product ------------------------------------------------------------------------------


def test_product_forecast_from_reservations(env: Any) -> None:
    kit = rental_product(env, "Forecast tent", units=3)
    for week in range(1, 9):  # weekend rentals over the last two months
        friday = NOW - _days(7 * week + 3)
        rental_order(env, kit, friday, friday + _days(2), qty=2).action_confirm()
    upcoming = rental_order(env, kit, NOW + _days(4), NOW + _days(6))
    upcoming.action_confirm()
    # another company's rental of the same (shared) product does not count
    eu = eu_env(env)
    other = rental_order(eu, kit, NOW + _days(4), NOW + _days(6), partner=customer(eu, "EU renter"))
    other.action_confirm()
    fc = A.product_forecast(env, kit, 4, NOW.date())
    assert fc["history_days"] == 7 * A.HISTORY_WEEKS and fc["units"] == 3 and len(fc["weeks"]) == 4
    assert fc["weeks"][0]["booked_unit_days"] == 3  # one unit, three days (the EU rental is not counted)
    assert sum(fc["recent_weeks"]) > 0 and fc["weeks"][0]["unit_days"] > 0
    assert sum(d["booked"] for d in fc["days"][:7]) == 3
    view = A.forecast_view(env, kit, 2)
    assert view["run_id"] and view["ai_generated"] and len(view["weeks"]) == 2
    run = env["ai_core.run"].elevated().browse([view["run_id"]])
    assert run.feature == A.FORECAST and run.res_id == kit.ids[0] and run.provider == "Tessera (in-process)"
    with pytest.raises(UserError):
        A.product_forecast(env, env["product.product"].create({"name": "Sold only"}))
    with pytest.raises(UserError):
        A.product_forecast(env, None)  # type: ignore[arg-type]
    # a product of another company is out of reach
    eu_kit = rental_product(eu, "EU only kit", company_id=eu.company.ids[0])
    us_user = env.with_company(env.ref("core.main_company").ids[0])
    with pytest.raises(AccessError):
        A.product_forecast(env.with_user(_sales_user(env).ids[0]), us_user["product.product"].browse(eu_kit.ids))


def _sales_user(env: Any) -> Any:
    users = env["core.user"].elevated().with_context(active_test=False)
    found = users.search([("login", "=", "rental-ai-rep@test")], limit=1)
    return found or users.create(
        {
            "name": "Rental rep",
            "login": "rental-ai-rep@test",
            "company_id": env.ref("core.main_company").ids[0],
            "company_ids": [("set", env.ref("core.main_company").ids)],
            "group_ids": [("set", [env.ref("sale.group_sale_user").ids[0]])],
        }
    )


def test_forecast_narrative_is_grounded(env: Any) -> None:
    kit = rental_product(env, "Narrated kit", units=4)
    rental_order(env, kit, NOW - _days(10), NOW - _days(8), qty=2).action_confirm()
    fc = A.product_forecast(env, kit, 2)
    first = fc["weeks"][0]
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": [{"match": {"feature": A.FORECAST}, "respond": {"json": {
        "narrative": f"Next week about {first['unit_days']} unit-days are expected. Demand will triple to 99 units."}}}]}})  # fmt: skip
    result = run_feature(env, A.FORECAST, {"weeks": 2}, record=kit, **HANDLERS[A.FORECAST](env, kit, {"weeks": 2}))
    assert result.data["removed_numbers"] == ["99"] and str(first["unit_days"]) in result.data["narrative"]
    assert not result.proposals and result.run.status == "ok"


# -- usage alerts on real rentals ----------------------------------------------------------------------------------


def _late_rentals(env: Any, kit: Any, partner: Any, count: int = 3) -> list[Any]:
    orders = []
    for k in range(count):
        start = NOW - _days(60 - 15 * k)
        order = rental_order(env, kit, start, start + _days(2), partner=partner)
        order.action_confirm()
        line = rental_line(order)
        line.action_pickup(when=start.isoformat())
        line.action_return(when=(start + _days(2) + dt.timedelta(hours=7)).isoformat())
        orders.append(order)
    return orders


def test_usage_check_raises_alerts_once_and_notifies(env: Any) -> None:
    company = env.ref("core.main_company").ids[0]
    kit = rental_product(env, "Alert kit", units=4, extra_hourly=10.0)
    late_customer = customer(env, "Always Late Ltd")
    orders = _late_rentals(env, kit, late_customer)
    for k in range(8):  # the product's usual short bookings
        start = NOW - _days(120 - 9 * k)
        rental_order(env, kit, start, start + _days(1)).action_confirm()
    long = rental_order(env, kit, NOW - _days(5), NOW + _days(20))  # 25 days where 1 is usual
    long.action_confirm()
    facts = A.usage_facts(env, company, NOW)
    assert {r["line_id"] for r in facts["rentals"]} >= {rental_line(o).ids[0] for o in orders}
    out = A.check_company(env, company, NOW)
    assert out["new"] >= 2
    alerts = env["sale.rental_anomaly"].search([("company_id", "=", company), ("product_id", "=", kit.ids[0])])
    kinds = {a.kind: a for a in alerts}
    assert {"late_returns", "long_booking"} <= set(kinds)
    late = kinds["late_returns"]
    assert late.partner_id == late_customer and late.details["late"] == 3 and late.state == "open"
    assert late.describe() == "Always Late Ltd returned 3 of their last 3 rentals late (up to 7 h)"
    assert kinds["long_booking"].order_id == long and kinds["long_booking"].details["days"] == 25.0
    notes = (
        env["core.notification"]
        .elevated()
        .search([("model", "=", "sale.rental_anomaly"), ("res_id", "in", alerts.ids)])
    )
    assert notes and all(n.user_id.ids == [env.uid] for n in notes)  # the orders' salesperson
    run = env["ai_core.run"].elevated().search([("feature", "=", A.ANOMALY)], order="id desc", limit=1)
    assert run.status == "ok" and run.company_id.ids == [company]
    # the same subjects are not raised twice, even once dismissed
    assert A.check_company(env, company, NOW)["new"] == 0
    view = A.alerts_view(env, order_id=orders[0].ids[0])
    assert [a["kind"] for a in view["alerts"]] == ["late_returns"] and view["ai_generated"]
    late.with_env(env.with_user(_sales_user(env).ids[0])).action_dismiss()
    assert late.state == "dismissed" and A.check_company(env, company, NOW)["new"] == 0
    assert not A.alerts_view(env, order_id=orders[0].ids[0])["alerts"]
    plain = env["core.user"].elevated().create({"name": "No sales", "login": "rental-plain@test"})
    with pytest.raises((AccessError, UserError)):
        late.with_env(env.with_user(plain.ids[0])).action_dismiss()
    # flags are notifications only: no order or rental changed
    assert all(rental_line(o).rental_status == "returned" for o in orders)


def test_alert_texts_and_company_isolation(env: Any) -> None:
    kit = rental_product(env, "Text kit", units=5)
    alerts = env["sale.rental_anomaly"].elevated()
    company = env.ref("core.main_company").ids[0]
    base = {"company_id": company, "product_id": kit.ids[0], "detected_on": NOW.date(), "score": 1.0}
    spike = alerts.create(
        {**base, "kind": "utilisation_spike", "key": "s", "details": {"actual": 30.0, "expected": 14.0}}
    )
    drop = alerts.create({**base, "kind": "utilisation_drop", "key": "d", "details": {"actual": 2.0, "expected": 14.0}})
    assert spike.summary == "Text kit: 30.0 unit-days out in the last 7 days, 14.0 expected"
    assert spike.name == spike.summary
    assert drop.describe() == "Text kit: only 2.0 unit-days out in the last 7 days, 14.0 expected"
    assert A.alert_dict(spike)["kind_label"] == "Utilisation spike"
    # another company's user sees none of them
    eu = env.ref("core.demo_company_eu")
    eu_user = (
        env["core.user"]
        .elevated()
        .create(
            {
                "name": "EU rental rep",
                "login": "rental-eu-ai@test",
                "company_id": eu.ids[0],
                "company_ids": [("set", eu.ids)],
                "group_ids": [("set", [env.ref("sale.group_sale_all").ids[0]])],
            }
        )
    )
    as_eu = env.with_user(eu_user.ids[0]).with_company(eu.ids[0])
    assert not as_eu["sale.rental_anomaly"].search([("id", "in", [spike.ids[0], drop.ids[0]])])
    assert all(r["product_id"] != kit.ids[0] for r in A.usage_facts(env, eu.ids[0], NOW)["rentals"])


def test_alert_narration_and_job_kill_switch(env: Any, system_env: Any) -> None:
    company = env.ref("core.main_company").ids[0]
    kit = rental_product(env, "Job kit", units=2, extra_hourly=5.0)
    _late_rentals(env, kit, customer(env, "Late Again SA"))
    A.check_company(env, company, NOW)
    alert = env["sale.rental_anomaly"].search([("product_id", "=", kit.ids[0])], limit=1)
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": [{"match": {"feature": A.ANOMALY}, "respond": {"json": {
        "explanation": "3 of 3 rentals came back late. Charge a 250 penalty."}}}]}})  # fmt: skip
    result = run_feature(env, A.ANOMALY, {}, record=alert, **HANDLERS[A.ANOMALY](env, alert, {}))
    assert result.data["removed_numbers"] == ["250"] and not result.proposals
    with pytest.raises(UserError):
        HANDLERS[A.ANOMALY](env, kit, {})
    assert A.usage_anomaly_job(system_env, [company])[str(company)]["rentals"] >= 3
    sched = env["core.scheduled_job"].elevated().search([("job_name", "=", "sale_rental.ai_usage_anomaly")])
    assert sched and sched.cron == "43 5 * * *"
    env["ai_core.feature"].elevated().search([("name", "=", A.ANOMALY)]).set_enabled(company, False)
    system_env.cache.invalidate()
    assert A.usage_anomaly_job(system_env, [company]) == {str(company): {"skipped": True}}
    with pytest.raises(FeatureDisabled):
        A.alerts_view(env, product_id=kit.ids[0])
    env["ai_core.feature"].elevated().search([("name", "=", A.FORECAST)]).set_enabled(company, False)
    with pytest.raises(FeatureDisabled):
        A.forecast_view(env, kit)
    assert MockAdapter.captured[-1:] and len(MockAdapter.captured) == 1  # only the narration called the gateway


def test_rental_ai_api(admin: Session) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        kit = rental_product(e, "API forecast kit", units=2)
        order = rental_order(e, kit, NOW - _days(9), NOW - _days(7))
        order.action_confirm()
        e.cr.commit()
        pid, oid, company = kit.ids[0], order.ids[0], e.ref("core.main_company").ids[0]
    try:
        _rental_api_checks(admin, pid, company)
    finally:
        with environment_for() as e:  # committed for the API: released again for the other tests
            e["sale.quotation"].browse([oid]).write({"state": "cancelled"})
            e.cr.commit()


def _rental_api_checks(admin: Session, pid: int, company: int) -> None:
    ok = admin.get(f"/api/v1/sale_rental/ai/products/{pid}/forecast", params={"weeks": 3})
    assert ok.status_code == 200, ok.text
    assert len(ok.json()["weeks"]) == 3 and ok.json()["run_id"]
    assert admin.get(f"/api/v1/sale_rental/ai/products/{pid}/forecast", params={"weeks": 40}).status_code == 422
    assert admin.get("/api/v1/sale_rental/ai/products/999999999/forecast").status_code == 404
    assert admin.get("/api/v1/sale_rental/ai/alerts", params={"product_id": pid}).json()["alerts"] == []
    assert admin.post("/api/v1/sale_rental/ai/alerts/999999999/dismiss").status_code == 404
    for name, url in (
        (A.FORECAST, f"/api/v1/sale_rental/ai/products/{pid}/forecast"),
        (A.ANOMALY, "/api/v1/sale_rental/ai/alerts"),
    ):
        flag = f"/api/v1/ai/admin/features/{name}/flag"
        admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
        try:
            off = admin.get(url)
            assert off.status_code == 404 and off.json()["code"] == "feature_disabled"
        finally:
            admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)
    flag = f"/api/v1/ai/admin/features/{A.ANOMALY}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        assert admin.post("/api/v1/sale_rental/ai/alerts/1/dismiss").json()["code"] == "feature_disabled"
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


# -- evaluation gate -------------------------------------------------------------------------------------------------


def test_rental_evaluations_pass_in_replay(env: Any) -> None:
    specs = EV.feature_specs(modules=["sale_rental"])
    assert {s.name for s in specs} == {A.FORECAST, A.ANOMALY}
    assert EV.completeness(specs) == []
    for spec in specs:
        report = EV.evaluate(env, spec)
        assert report.passed, (spec.name, report.failures, report.metrics)
