"""Access rights and company isolation of the subscription models, KPIs, churn features and the demo."""

from __future__ import annotations

from typing import Any

import pytest

from modules.account.tests.common import eu_env
from modules.sale_subscription.metrics import churn_features, subscription_metrics
from modules.sale_subscription.tests.common import D, at, customer, plan, recurring, run, subscription
from tessera.exceptions import AccessError

pytestmark = pytest.mark.db


def _user(env: Any, login: str, groups: list[str], company: Any | None = None) -> Any:
    company = company or env.company
    return env["core.user"].create(
        {
            "name": login,
            "login": login,
            "company_id": company.ids[0],
            "company_ids": [("set", company.ids)],
            "group_ids": [("set", [env.ref(g).ids[0] for g in groups])],
        }
    )


def test_configuration_rights(env: Any) -> None:
    seller = env.with_user(_user(env, "subs-seller@subs.test", ["sale.group_sale_user"]).ids[0])
    assert seller["sale.subscription_plan"].search_count([]) >= 3
    assert seller["sale.subscription_close_reason"].search_count([]) >= 5
    with pytest.raises(AccessError):
        seller["sale.subscription_plan"].create({"name": "Mine"})
    with pytest.raises(AccessError):
        seller["sale.subscription_close_reason"].search([], limit=1).write({"name": "x"})
    with pytest.raises(AccessError):
        seller["sale.subscription_log"].create({})
    manager = env.with_user(_user(env, "subs-manager@subs.test", ["sale.group_sale_manager"]).ids[0])
    assert manager["sale.subscription_plan"].create({"name": "Manager plan", "billing_period_unit": "week"})
    plain = env.with_user(_user(env, "subs-plain@subs.test", ["core.group_user"]).ids[0])
    with pytest.raises(AccessError):
        plain["sale.subscription_plan"].search([])
    with pytest.raises(AccessError):
        plain["sale.subscription_log"].search([])


def test_salespeople_see_the_history_of_their_own_subscriptions(env: Any) -> None:
    mine_user = _user(env, "own-seller@subs.test", ["sale.group_sale_user"])
    other_user = _user(env, "other-seller@subs.test", ["sale.group_sale_user"])
    p, product = plan(env), recurring(env)
    mine = subscription(
        env, customer(env), p, [{"product_id": product.ids[0]}], D(2026, 3, 1), user_id=mine_user.ids[0]
    )
    theirs = subscription(
        env, customer(env), p, [{"product_id": product.ids[0]}], D(2026, 3, 1), user_id=other_user.ids[0]
    )
    seller = env.with_user(mine_user.ids[0])
    logs = seller["sale.subscription_log"].search([("subscription_id", "in", [mine.ids[0], theirs.ids[0]])])
    assert logs.mapped("subscription_id").ids == mine.ids
    assert not seller["sale.quotation"].search([("id", "=", theirs.ids[0])])
    # a salesperson can pause their own subscription, not another one's
    seller["sale.quotation"].browse(mine.ids).with_context(subscription_date="2026-03-05").action_pause()
    assert mine.subscription_state == "paused"
    with pytest.raises(AccessError):
        seller["sale.quotation"].browse(theirs.ids).action_pause()


def test_company_isolation(env: Any) -> None:
    eu = eu_env(env)
    eu_plan = eu["sale.subscription_plan"].create({"name": "EU monthly", "company_id": eu.company.ids[0]})
    product = recurring(eu, "EU seat", 50)
    eu_sub = subscription(
        eu, customer(eu, "Berlin GmbH"), eu_plan, [{"product_id": product.ids[0], "price_unit": 50}], D(2026, 3, 1)
    )
    run(eu, D(2026, 3, 1))
    assert eu_sub.company_id == eu.company and eu_sub.subscription_log_ids.company_id == eu.company
    assert eu_sub.subscription_move_ids.company_id == eu.company
    us_user = _user(env, "us-subs@subs.test", ["sale.group_sale_manager"], env.ref("core.main_company"))
    us = env.with_user(us_user.ids[0])
    assert not us["sale.quotation"].search([("id", "=", eu_sub.ids[0])])
    assert not us["sale.subscription_log"].search([("subscription_id", "=", eu_sub.ids[0])])
    assert not us["sale.subscription_plan"].search([("id", "=", eu_plan.ids[0])])
    assert us["sale.subscription_plan"].search([("id", "=", env.ref("sale_subscription.plan_monthly").ids[0])])
    us_metrics = subscription_metrics(env.ref("core.main_company"), D(2026, 3, 1), D(2026, 3, 31))
    eu_metrics = subscription_metrics(eu.company, D(2026, 3, 1), D(2026, 3, 31))
    assert eu_metrics["new"] == 50.0 and eu_metrics["currency"] == "EUR"
    assert us_metrics["new"] != 50.0 or us_metrics["company"] != eu_metrics["company"]


def test_metrics_and_churn_features(env: Any) -> None:
    e = eu_env(env)
    company = e.company
    p = plan(e)
    product = recurring(e, "Metric seat", 100)
    a = subscription(
        e, customer(e, "A"), p, [{"product_id": product.ids[0], "quantity": 2, "price_unit": 100}], D(2026, 1, 1)
    )
    b = subscription(e, customer(e, "B"), p, [{"product_id": product.ids[0], "price_unit": 100}], D(2026, 1, 15))
    c = subscription(e, customer(e, "C"), p, [{"product_id": product.ids[0], "price_unit": 100}], D(2026, 2, 1))
    run(e, D(2026, 2, 1))
    run(e, D(2026, 2, 15))
    change = at(e, D(2026, 2, 10))["sale.quotation"].browse(
        [a.with_env(at(e, D(2026, 2, 10))).action_upsell()["upsell_id"]]
    )
    change.line_ids.write({"quantity": 3})
    change.action_confirm()
    b.with_env(at(e, D(2026, 2, 20))).action_close(reason="price", when="now")
    change = at(e, D(2026, 2, 25))["sale.quotation"].browse(
        [c.with_env(at(e, D(2026, 2, 25))).action_upsell()["upsell_id"]]
    )
    change.line_ids.write({"price_unit": 60})
    change.action_confirm()
    m = subscription_metrics(company, D(2026, 2, 1), D(2026, 2, 28))
    assert (m["mrr_start"], m["active_start"]) == (300.0, 2)
    assert (m["new"], m["expansion"], m["contraction"], m["churn"]) == (100.0, 100.0, -40.0, -100.0)
    assert m["mrr_end"] == 360.0 and m["active_end"] == 2 and m["arr_end"] == 4320.0
    assert m["churned_count"] == 1 and m["churn_rate"] == 0.5 and m["revenue_churn_rate"] == round(100 / 300, 4)
    assert m["net_change"] == 60.0 and m["net_revenue_retention"] == round(260 / 300, 4)
    q1 = subscription_metrics(company, D(2026, 1, 1), D(2026, 3, 31))
    assert [row["month"] for row in q1["monthly"]] == ["2026-01", "2026-02", "2026-03"]
    assert q1["monthly"][0]["new"] == 300.0 and q1["monthly"][1]["churned_count"] == 1
    assert q1["monthly"][0]["churn_rate"] is None  # nobody active at the start of January
    via_api = e["sale.subscription_log"].subscription_metrics("2026-02-01", "2026-02-28")
    assert via_api["mrr_end"] == 360.0
    features = churn_features(b, D(2026, 3, 1))
    assert features["churned"] and features["close_reason"] == "Too expensive" and features["tenure_days"] == 36
    assert features["invoice_count"] == 2 and features["credit_note_count"] == 1
    fa = churn_features(a, D(2026, 3, 1))
    assert fa["upsell_count"] == 1 and fa["mrr"] == 300.0 and fa["unpaid_invoice_count"] == 3
    assert fa["overdue_amount"] > 0 and fa["days_since_last_payment"] is None and not fa["has_payment_method"]
    fc = churn_features(c, D(2026, 3, 1))
    assert fc["downsell_count"] == 1 and fc["mrr_change_90d"] == -40.0 and fc["renewals"] == 0


def test_demo_scenario(env: Any, system_env: Any) -> None:
    from modules.sale_subscription.demo import seed_subscriptions

    created = seed_subscriptions(system_env)
    assert created == 8
    assert seed_subscriptions(system_env) == 0
    subs = system_env["sale.quotation"].search(
        [("client_reference", "like", "DEMO-SUB-"), ("upsell_of_id", "=", None), ("renewal_of_id", "=", None)]
    )
    assert len(subs) == 8
    states = {s.client_reference: s.subscription_state for s in subs}
    assert states["DEMO-SUB-02"] == "churned" and states["DEMO-SUB-04"] == "paused"
    assert states["DEMO-SUB-01"] == "progress" and states["DEMO-SUB-07"] == "progress"
    logs = system_env["sale.subscription_log"].search([("subscription_id", "in", subs.ids)])
    assert {"new", "upsell", "downsell", "churn"} <= set(logs.mapped("event_type"))
    first = subs.filtered(lambda s: s.client_reference == "DEMO-SUB-01")
    assert first.payment_token_id and first.subscription_move_ids
    assert subs.filtered(lambda s: s.client_reference == "DEMO-SUB-05").renewal_ids
