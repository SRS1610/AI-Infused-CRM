from __future__ import annotations

import pytest

from modules.sale_subscription.tests.common import D, customer, docs, history, plan, recurring, run, subscription

pytestmark = pytest.mark.db


def test_monthly_basic(env):
    p = plan(env)
    sub = subscription(env, customer(env), p, [{"product_id": recurring(env).ids[0], "quantity": 1}], D(2026, 1, 31))
    assert sub.is_subscription and sub.subscription_state == "progress"
    run(env, D(2026, 1, 31))
    run(env, D(2026, 2, 28))
    moves = docs(sub)
    assert [(m.subscription_period_start, m.subscription_period_end, m.amount_total) for m in moves] == [
        (D(2026, 1, 31), D(2026, 2, 27), 100.0),
        (D(2026, 2, 28), D(2026, 3, 30), 100.0),
    ]
    assert history(sub) == [("2026-01-31", "new", 100.0)]
