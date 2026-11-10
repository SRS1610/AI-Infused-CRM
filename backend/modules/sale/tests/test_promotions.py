"""Promotions: conditions, the four kinds of reward and their taxes, codes, limits, idempotent application."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from modules.sale.tests.common import customer, no_promotions, order, product, product_lines, sale_tax
from tessera.exceptions import UserError, ValidationError

pytestmark = pytest.mark.db


@pytest.fixture
def setup(env: Any) -> dict[str, Any]:
    no_promotions(env)
    t20, t10 = sale_tax(env, 20), sale_tax(env, 10)
    a = product(env, "Promo A", price=100, taxes=t20)
    b = product(env, "Promo B", price=50, taxes=t10)
    q = order(env, customer(env), [(a, 2), (b, 1)])
    return {"t20": t20, "t10": t10, "a": a, "b": b, "q": q}


def _promo(env: Any, reward: dict[str, Any], rules: list[dict[str, Any]] | None = None, **vals: Any) -> Any:
    return env["sale.promotion"].create(
        {
            "name": vals.pop("name", "Test promotion"),
            "reward_ids": [("create", reward)],
            "rule_ids": [("create", r) for r in (rules or [])],
            **vals,
        }
    )


def _rewards(q: Any) -> Any:
    return q.line_ids.filtered(lambda line: line.is_reward_line)


def test_percentage_on_order_keeps_each_tax(env: Any, setup: dict[str, Any]) -> None:
    q = setup["q"]
    promo = _promo(env, {"reward_type": "discount_order", "discount_percent": 10}, [{"minimum_amount": 200}])
    result = q.action_apply_promotions()
    assert result["promotion_ids"] == promo.ids
    rewards = _rewards(q)
    by_tax = {line.tax_ids.ids[0]: line.price_unit for line in rewards}
    assert by_tax == {setup["t20"].ids[0]: -20.0, setup["t10"].ids[0]: -5.0}
    assert q.amount_untaxed == 225 and q.amount_tax == pytest.approx(40.5)
    assert q.promotion_ids == promo
    ids = rewards.ids
    q.action_apply_promotions()  # applying again changes nothing
    assert _rewards(q).ids == ids and q.amount_untaxed == 225
    product_lines(q).filtered(lambda line: line.product_id == setup["a"]).write({"quantity": 1})
    q.action_apply_promotions()  # 150 < 200: no longer eligible
    assert not _rewards(q) and q.amount_untaxed == 150


def test_percentage_on_cheapest_line(env: Any, setup: dict[str, Any]) -> None:
    _promo(env, {"reward_type": "discount_cheapest", "discount_percent": 50})
    setup["q"].action_apply_promotions()
    reward = _rewards(setup["q"])
    assert reward.price_unit == -25 and reward.tax_ids == setup["t10"] and "Promo B" in reward.name


def test_fixed_amount_is_split_over_the_taxes(env: Any, setup: dict[str, Any]) -> None:
    _promo(env, {"reward_type": "fixed_amount", "fixed_amount": 30})
    q = setup["q"]
    q.action_apply_promotions()
    by_tax = {line.tax_ids.ids[0]: line.price_unit for line in _rewards(q)}
    assert by_tax == {setup["t20"].ids[0]: -24.0, setup["t10"].ids[0]: -6.0}
    assert q.amount_untaxed == 220
    capped = _promo(env, {"reward_type": "fixed_amount", "fixed_amount": 10000}, name="Too generous")
    q.action_apply_promotions()
    assert q.amount_untaxed == 0  # never below zero
    capped.write({"active": False})


def test_free_product_when_buying_enough(env: Any, setup: dict[str, Any]) -> None:
    q = setup["q"]
    _promo(
        env,
        {"reward_type": "free_product", "free_product_id": setup["b"].ids[0], "free_quantity": 2},
        [{"minimum_qty": 3, "product_ids": [("set", setup["a"].ids)]}],
    )
    q.action_apply_promotions()
    assert not _rewards(q)  # two units of A only
    product_lines(q).filtered(lambda line: line.product_id == setup["a"]).write({"quantity": 3})
    q.action_apply_promotions()
    free = _rewards(q)
    assert free.product_id == setup["b"] and free.quantity == 2 and free.discount == 100
    assert free.price_subtotal == 0 and q.amount_untaxed == 350


def test_category_condition(env: Any, setup: dict[str, Any]) -> None:
    parent = env["product.category"].create({"name": "Promo parent"})
    child = env["product.category"].create({"name": "Promo child", "parent_id": parent.ids[0]})
    _promo(
        env,
        {"reward_type": "discount_order", "discount_percent": 5},
        [{"category_ids": [("set", parent.ids)]}],
    )
    q = setup["q"]
    q.action_apply_promotions()
    assert not _rewards(q)
    setup["b"].write({"category_id": child.ids[0]})
    q.action_apply_promotions()
    assert _rewards(q)


def test_codes(env: Any, setup: dict[str, Any]) -> None:
    q = setup["q"]
    promo = _promo(env, {"reward_type": "discount_order", "discount_percent": 10}, program_type="code", code=" save10 ")
    assert promo.code == "SAVE10"
    q.action_apply_promotions()
    assert not _rewards(q)  # codes are not automatic
    with pytest.raises(UserError, match="Unknown promotion code"):
        q.action_apply_promotions(code="NOPE")
    q.action_apply_promotions(code="save10")
    assert _rewards(q) and q.promotion_code_ids == promo
    q.action_apply_promotions()  # the code stays on the order
    assert _rewards(q)
    strict = _promo(
        env,
        {"reward_type": "discount_order", "discount_percent": 10},
        [{"minimum_amount": 10000}],
        program_type="code",
        code="BIG",
        name="Big orders",
    )
    with pytest.raises(UserError, match="conditions"):
        q.action_apply_promotions(code="BIG")
    assert strict not in q.promotion_code_ids
    with pytest.raises(ValidationError):
        _promo(env, {"reward_type": "discount_order", "discount_percent": 10}, program_type="code", name="No code")


def test_limits_dates_and_companies(env: Any, setup: dict[str, Any]) -> None:
    q = setup["q"]
    promo = _promo(env, {"reward_type": "discount_order", "discount_percent": 10}, max_usage=1)
    q.action_apply_promotions()
    q.action_confirm()
    assert promo.usage_count == 1
    other = order(env, customer(env, "Second buyer"), [(setup["a"], 1)])
    other.action_apply_promotions()
    assert not _rewards(other)  # used up
    promo.write({"max_usage": 0, "date_to": env.today() - dt.timedelta(days=1)})
    other.action_apply_promotions()
    assert not _rewards(other)  # ended
    promo.write({"date_to": None, "date_from": env.today() + dt.timedelta(days=1)})
    other.action_apply_promotions()
    assert not _rewards(other)  # not started
    eu = env["core.company"].search([("chart_template", "=", "eu")], limit=1) or env["core.company"].elevated().search(
        [("chart_template", "=", "eu")], limit=1
    )
    promo.elevated().write({"date_from": None, "company_id": eu.ids[0]})
    other.action_apply_promotions()
    assert not _rewards(other)  # another company's promotion
    promo.elevated().write({"company_id": None})
    other.action_apply_promotions()
    assert _rewards(other)
    with pytest.raises(ValidationError):
        promo.write({"date_from": env.today(), "date_to": env.today() - dt.timedelta(days=2)})


def test_rewards_are_validated(env: Any) -> None:
    with pytest.raises(ValidationError):
        _promo(env, {"reward_type": "discount_order", "discount_percent": 0})
    with pytest.raises(ValidationError):
        _promo(env, {"reward_type": "fixed_amount", "fixed_amount": 0})
    with pytest.raises(ValidationError):
        _promo(env, {"reward_type": "free_product"})
    with pytest.raises(ValidationError):
        _promo(env, {"reward_type": "discount_order", "discount_percent": 5}, [{"minimum_amount": -1}])


def test_locked_or_invoiced_orders_keep_their_rewards(env: Any, setup: dict[str, Any]) -> None:
    q = setup["q"]
    _promo(env, {"reward_type": "discount_order", "discount_percent": 10})
    q.action_apply_promotions()
    q.action_confirm()
    q.action_create_invoice()
    with pytest.raises(UserError, match="already invoiced"):
        q.action_apply_promotions()
    q.action_lock()
    with pytest.raises(UserError, match="locked"):
        q.action_apply_promotions()
    cancelled = order(env, customer(env, "Cancelled buyer"), [(setup["a"], 1)])
    cancelled.action_cancel()
    with pytest.raises(UserError, match="quotations and sales orders"):
        cancelled.action_apply_promotions()
