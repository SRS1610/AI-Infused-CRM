"""Rental prices: best combination of pricing rules for a duration, wall-clock durations across daylight-saving
changes, pricelist-specific rules, typed prices, late fees."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from modules.sale_rental.pricing import Block, best_price, describe, late_fee, wall_clock_hours
from modules.sale_rental.tests.common import at, rental_line, rental_order, rental_product
from tessera.exceptions import UserError, ValidationError

DAY, WEEK = Block(1, 24, 100.0, "1 day"), Block(2, 168, 500.0, "1 week")


def test_best_price_combines_blocks() -> None:
    assert best_price([DAY, WEEK], 24 * 9) == (700.0, {2: 1, 1: 2})  # 9 days = 1 week + 2 days
    assert best_price([DAY, WEEK], 24 * 6) == (500.0, {2: 1})  # 6 days cost more than a week
    assert best_price([DAY, WEEK], 24 * 12) == (1000.0, {2: 2})  # 1 week + 5 days > 2 weeks
    assert best_price([DAY, WEEK], 3) == (100.0, {1: 1})  # a started day counts
    assert best_price([DAY], 0) == (0.0, {})
    assert best_price([], 10) == (0.0, {})
    hour = Block(3, 4, 30.0, "4 hours")
    assert best_price([hour, DAY], 8) == (60.0, {3: 2})
    assert best_price([hour, DAY], 20) == (100.0, {1: 1})  # 5 x 4 hours = 150 > a day
    assert describe([DAY, WEEK], {1: 2, 2: 1}) == "1 x 1 week + 2 x 1 day"
    with pytest.raises(ValueError):
        best_price([DAY], 24 * 366 * 6)


def test_wall_clock_hours_ignore_daylight_saving_switches() -> None:
    # Europe/Paris goes from UTC+1 to UTC+2 on 2026-03-29 and back on 2026-10-25
    spring_start = dt.datetime(2026, 3, 28, 9, 0, tzinfo=dt.UTC)  # 10:00 local
    spring_end = dt.datetime(2026, 3, 29, 8, 0, tzinfo=dt.UTC)  # 10:00 local, 23 hours later
    assert wall_clock_hours(spring_start, spring_end, "Europe/Paris") == 24
    assert wall_clock_hours(spring_start, spring_end, "UTC") == 23
    autumn_start = dt.datetime(2026, 10, 24, 8, 0, tzinfo=dt.UTC)  # 10:00 local
    autumn_end = dt.datetime(2026, 10, 25, 9, 0, tzinfo=dt.UTC)  # 10:00 local, 25 hours later
    assert wall_clock_hours(autumn_start, autumn_end, "Europe/Paris") == 24
    assert wall_clock_hours(autumn_start, autumn_end, "Not/AZone") == 25  # unknown zone: UTC
    assert wall_clock_hours(spring_end, spring_start, "UTC") == 0
    assert wall_clock_hours(spring_start, spring_start + dt.timedelta(minutes=1), "UTC") == 1
    # the clocks go back during a 30-minute rental (01:50 → 01:20 local): the elapsed time counts
    back = dt.datetime(2026, 10, 24, 23, 50, tzinfo=dt.UTC)
    assert wall_clock_hours(back, back + dt.timedelta(minutes=30), "Europe/Paris") == 1


def test_late_fee_formula() -> None:
    assert late_fee(0, 10, 50) == 0
    assert late_fee(5, 0, 50) == 50  # a started day
    assert late_fee(25, 0, 50) == 100
    assert late_fee(5, 10, 0) == 50
    assert late_fee(3, 10, 50) == 30
    assert late_fee(7, 10, 50) == 50  # the hours of a day cost at most a day
    assert late_fee(26, 10, 50) == 70  # one day + 2 hours


@pytest.mark.db
def test_order_line_price_from_the_best_rule(env: Any) -> None:
    kit = rental_product(env)
    order = rental_order(env, kit, at(1), at(10), qty=2)
    line = rental_line(order)
    assert line.price_unit == 700 and line.price_subtotal == 1400 and not line.price_manual
    assert line.rental_price_detail == "1 x 1 week + 2 x 1 day"
    assert "Rental from 2026-12-01 09:00 to 2026-12-10 09:00" in line.name
    line.write({"return_date": at(13)})  # 12 days: two weeks are cheaper
    assert line.price_unit == 1000 and line.rental_price_detail == "2 x 1 week"
    assert "2026-12-13 09:00" in line.name
    line.write({"price_unit": 900})
    assert line.price_manual
    line.write({"return_date": at(5)})
    assert line.price_unit == 900  # a typed price stays
    line.write({"price_unit": None})
    assert line.price_unit == 400
    line.write({"name": "Custom text"})
    line.write({"return_date": at(6)})
    assert line.name == "Custom text"  # a typed description is kept


@pytest.mark.db
def test_duration_follows_the_salespersons_time_zone(env: Any) -> None:
    kit = rental_product(env, prices=[(1, "hour", 10.0), (1, "day", 100.0)])
    env.user.elevated().write({"tz": "Europe/Paris"})
    # 10:00 → 10:00 local over the spring switch: one day, not 23 hours
    order = rental_order(
        env, kit, dt.datetime(2026, 3, 28, 9, tzinfo=dt.UTC), dt.datetime(2026, 3, 29, 8, tzinfo=dt.UTC)
    )
    assert rental_line(order).price_unit == 100
    assert "2026-03-28 10:00" in rental_line(order).name
    env.user.elevated().write({"tz": "UTC"})
    other = rental_order(
        env, kit, dt.datetime(2026, 3, 28, 9, tzinfo=dt.UTC), dt.datetime(2026, 3, 29, 8, tzinfo=dt.UTC)
    )
    assert rental_line(other).price_unit == 100  # 23 hours: a day is cheaper than 23 x 10


@pytest.mark.db
def test_pricelist_rules_and_fallback_price(env: Any) -> None:
    kit = rental_product(env)
    pricelist = env["product.pricelist"].create(
        {"name": "Rental partners", "currency_id": env.company.currency_id.ids[0]}
    )
    env["sale.rental_pricing"].create(
        {"product_id": kit.ids[0], "duration": 1, "unit": "day", "price": 80, "pricelist_id": pricelist.ids[0]}
    )
    order = rental_order(env, kit, at(1), at(3), pricelist_id=pricelist.ids[0])
    assert rental_line(order).price_unit == 160  # only the pricelist's rules
    plain = rental_order(env, kit, at(1), at(3))
    assert rental_line(plain).price_unit == 200
    bare = rental_product(env, "No rules", prices=[])
    assert rental_line(rental_order(env, bare, at(1), at(3))).price_unit == 100  # sales price per day
    with pytest.raises(ValidationError):
        env["sale.rental_pricing"].create({"product_id": kit.ids[0], "duration": 0, "unit": "day", "price": 1})
    eur = env.ref("core.EUR")
    with pytest.raises(ValidationError, match="pricelist's currency"):
        env["sale.rental_pricing"].create(
            {"product_id": kit.ids[0], "price": 1, "pricelist_id": pricelist.ids[0], "currency_id": eur.ids[0]}
        )


@pytest.mark.db
def test_rule_in_another_currency_is_converted(env: Any) -> None:
    eur = env.ref("core.EUR")
    kit = rental_product(env, prices=[])
    env["sale.rental_pricing"].create(
        {"product_id": kit.ids[0], "duration": 1, "unit": "day", "price": 92, "currency_id": eur.ids[0]}
    )
    line = rental_line(rental_order(env, kit, at(1), at(2)))
    assert line.currency_id == env.company.currency_id
    assert line.price_unit == pytest.approx(
        float(eur.convert(92, env.company.currency_id, env.company, dt.date(2026, 11, 11))), abs=0.02
    )


@pytest.mark.db
def test_rental_line_validation(env: Any) -> None:
    kit = rental_product(env)
    with pytest.raises(ValidationError, match="after the pickup"):
        rental_order(env, kit, at(5), at(4))
    with pytest.raises(ValidationError, match="whole units"):
        rental_order(env, kit, at(1), at(2), qty=1.5)
    plain = env["product.product"].create({"name": "Not for rent", "list_price": 5})
    with pytest.raises(ValidationError, match="cannot be rented"):
        rental_order(env, plain, at(1), at(2))
    with pytest.raises(UserError, match="five years"):
        rental_order(env, kit, at(1, year=2026), at(1, year=2032))
    # dates on a rentable product make it a rental; without dates it is sold
    order = env["sale.quotation"].create(
        {
            "partner_id": env.ref("contacts.partner_acme").ids[0],
            "line_ids": [
                ("create", {"product_id": kit.ids[0], "start_date": at(1), "return_date": at(2)}),
                ("create", {"product_id": kit.ids[0], "quantity": 1}),
            ],
        }
    )
    rented, sold = order.line_ids.filtered(lambda r: r.product_id == kit).sorted(lambda r: r.ids[0])
    assert rented.is_rental and not sold.is_rental and sold.price_unit == 50
    with pytest.raises(UserError, match="pickup and return dates"):
        order.write({"line_ids": [("create", {"product_id": kit.ids[0], "is_rental": True})]})
        order.action_confirm()
