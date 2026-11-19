"""Pickup, return, late fees, deposits, invoicing, the overdue job, usage history, access rights, company
isolation and the demo scenario."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from modules.account.chart import chart_ref
from modules.account.tests.common import eu_env
from modules.sale.tests.common import sale_tax
from modules.sale_rental.rental import mark_late, mark_late_job, rental_history
from modules.sale_rental.tests.common import at, rental_line, rental_order, rental_product
from tessera.exceptions import AccessError, UserError

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


def _charges(order: Any, kind: str) -> Any:
    return order.line_ids.filtered(lambda r: r.rental_charge == kind)


def test_deposit_line_follows_the_rental(env: Any) -> None:
    kit = rental_product(env, rental_deposit=150)
    order = rental_order(env, kit, at(1), at(3), qty=2)
    deposit = _charges(order, "deposit")
    assert deposit.quantity == 2 and deposit.price_unit == 150 and not deposit.tax_ids
    assert deposit.rental_origin_line_id == rental_line(order)
    assert deposit.product_id == env.ref("sale_rental.product_rental_deposit")
    assert order.amount_untaxed == 2 * 200 + 300
    rental_line(order).write({"quantity": 3})
    assert _charges(order, "deposit").quantity == 3
    kit.write({"rental_deposit": 0})
    rental_line(order).write({"return_date": at(4)})
    assert not _charges(order, "deposit")
    kit.write({"rental_deposit": 40})
    rental_line(order).write({"return_date": at(5)})
    assert _charges(order, "deposit").price_unit == 40
    # a copied order gets fresh deposits, not copies of the old ones
    copy = order.copy()
    assert len(_charges(copy, "deposit")) == 1
    assert _charges(copy, "deposit").rental_origin_line_id == rental_line(copy)
    # deleting the rental deletes its deposit
    rental_line(copy).unlink()
    assert not copy.line_ids


def test_pickup_return_on_time(env: Any) -> None:
    kit = rental_product(env, units=1, extra_hourly=10, extra_daily=60)
    order = rental_order(env, kit, at(1), at(3))
    line = rental_line(order)
    with pytest.raises(UserError, match="Confirm the order first"):
        line.action_pickup()
    order.action_confirm()
    with pytest.raises(UserError, match="not out with the customer"):
        line.action_return()
    order.action_rental_pickup()
    assert line.rental_status == "pickedup" and order.rental_status == "pickedup" and line.qty_delivered == 1
    assert line.pickup_datetime is not None
    with pytest.raises(UserError, match="Nothing is waiting for pickup"):
        order.action_rental_pickup()
    with pytest.raises(UserError, match="not waiting for pickup"):
        line.action_pickup()
    with pytest.raises(UserError, match="only its return date"):
        line.write({"quantity": 2})
    with pytest.raises(UserError, match="still with the customer"):
        order.action_cancel()
    line.action_return(when=at(2, 15).isoformat())  # early: the unit is free from then
    assert line.rental_status == "returned" and order.rental_status == "returned" and not line.late_fee
    assert line.rental_allocation_ids.date_end == at(2, 15)
    assert not _charges(order, "late_fee")
    with pytest.raises(UserError, match="Nothing is out"):
        order.action_rental_return()
    with pytest.raises(UserError, match="is returned"):
        line.write({"return_date": at(4)})
    nxt = rental_order(env, kit, at(2, 15), at(4))
    nxt.action_confirm()  # the early return freed the unit
    sold = env["sale.quotation_line"].create({"quotation_id": nxt.ids[0], "product_id": kit.ids[0], "quantity": 1})
    with pytest.raises(UserError, match="not a rental"):
        sold.action_pickup()


def test_late_return_adds_a_fee_line(env: Any) -> None:
    t20 = sale_tax(env, 20)
    kit = rental_product(env, units=1, extra_hourly=10, extra_daily=60, rental_deposit=100, taxes_id=[("set", t20.ids)])
    order = rental_order(env, kit, at(1), at(3), qty=1)
    order.action_confirm()
    line = rental_line(order)
    line.action_pickup(when=at(1).isoformat())
    with pytest.raises(UserError, match="before the pickup"):
        line.action_return(when=at(1, 8).isoformat())
    # the customer keeps it longer: extending the return date moves the reservation
    line.write({"return_date": at(4)})
    assert line.rental_allocation_ids.date_end == at(4) and line.rental_status == "pickedup"
    blocker = rental_order(env, kit, at(6), at(8))
    blocker.action_confirm()
    with pytest.raises(UserError, match="booked by another rental"):
        line.write({"return_date": at(7)})
    line.action_return(when=at(5, 11).isoformat())  # 1 day and 2 hours late: 60 + 2 x 10
    assert line.late_fee == 80 and line.returned_datetime == at(5, 11)
    fee = _charges(order, "late_fee")
    assert fee.price_unit == 80 and fee.quantity == 1 and fee.tax_ids == t20
    assert "26 h late" in fee.name and fee.product_id == env.ref("sale_rental.product_rental_late_fee")
    assert line.rental_allocation_ids.date_end == at(4)  # the planned end stays (the unit is back)
    deposit = _charges(order, "deposit")
    assert deposit.qty_returned == 1


def test_invoicing_rental_deposit_late_fee_and_deposit_refund(env: Any) -> None:
    t20 = sale_tax(env, 20)
    kit = rental_product(
        env, units=1, extra_daily=50, rental_deposit=100, taxes_id=[("set", t20.ids)], invoice_policy="delivery"
    )
    order = rental_order(env, kit, at(1), at(3))
    order.action_confirm()
    line = rental_line(order)
    assert line.invoice_policy == "order" and line.qty_to_invoice == 1  # invoiced by order, not on delivery
    invoice = env["account.move"].browse(order.action_create_invoice()["invoice_ids"])
    company = order.company_id
    items = {i.sale_line_ids.ids[0]: i for i in invoice.invoice_line_ids if i.sale_line_ids}
    deposit = _charges(order, "deposit")
    assert items[line.ids[0]].account_id == chart_ref(company, "income_rental")
    assert items[deposit.ids[0]].account_id == chart_ref(company, "customer_deposits")
    assert invoice.amount_untaxed == 300 and invoice.amount_tax == 40
    invoice.action_post()
    assert order.locked
    line.action_pickup(when=at(1).isoformat())
    line.action_return(when=at(3, 10).isoformat())  # one started day late
    assert not order.locked and order.invoice_status == "to_invoice"
    fee_invoice = env["account.move"].browse(order.action_create_invoice()["invoice_ids"])
    fee_item = fee_invoice.invoice_line_ids.filtered(lambda i: i.sale_line_ids)
    assert fee_item.price_unit == 50 and fee_item.account_id == chart_ref(company, "income_rental")
    credit = env["account.move"].browse(order.action_create_credit_note()["invoice_ids"])
    assert credit.move_type == "out_refund" and credit.amount_total == 100
    assert credit.invoice_line_ids.account_id == chart_ref(company, "customer_deposits")


def test_overdue_job_marks_late_and_notifies(env: Any) -> None:
    kit = rental_product(env, units=2)
    seller = _user(env, "rent-seller@rental.test", ["sale.group_sale_user"])
    order = rental_order(env, kit, at(1), at(3), user_id=seller.ids[0])
    order.action_confirm()
    rental_line(order).action_pickup(when=at(1).isoformat())
    on_time = rental_order(env, kit, at(1), at(9))
    on_time.action_confirm()
    rental_line(on_time).action_pickup(when=at(1).isoformat())
    assert mark_late(env, at(2)) == 0
    assert mark_late(env, at(4)) == 1
    assert rental_line(order).rental_status == "late" and order.rental_status == "late"
    assert rental_line(on_time).rental_status == "pickedup"
    note = (
        env["core.notification"].elevated().search([("user_id", "=", seller.ids[0]), ("title", "like", "Late rental")])
    )
    assert len(note) == 1 and order.name in note.title and note.res_id == order.ids[0]
    assert mark_late(env, at(5)) == 0  # notified once
    rental_line(order).action_return(when=at(5).isoformat())
    assert rental_line(order).rental_status == "returned"
    assert "late" in mark_late_job(env)


def test_rental_history_daily_utilisation(env: Any) -> None:
    kit = rental_product(env, units=2, extra_daily=10)
    a = rental_order(env, kit, at(1, 0), at(3, 0))
    a.action_confirm()
    rental_line(a).action_pickup(when=at(1, 0).isoformat())
    rental_line(a).action_return(when=at(3, 12).isoformat())
    b = rental_order(env, kit, at(2, 12), at(3, 0))
    b.action_confirm()
    days = rental_history(kit, dt.date(2026, 12, 1), dt.date(2026, 12, 4))
    assert [(d["date"], d["booked_units"], d["booked_hours"], d["rentals_started"]) for d in days] == [
        ("2026-12-01", 1, 24.0, 1),
        ("2026-12-02", 2, 36.0, 1),
        ("2026-12-03", 0, 0.0, 0),
        ("2026-12-04", 0, 0.0, 0),
    ]
    assert days[1]["utilisation"] == 0.75 and days[0]["units"] == 2
    assert [d["late_returns"] for d in days] == [0, 0, 1, 0]
    exposed = env["sale.rental_allocation"].rental_history(kit.ids[0], "2026-12-01", "2026-12-02")
    assert exposed == days[:2]
    with pytest.raises(UserError):
        env["sale.rental_allocation"].rental_history(kit.ids[0], "2026-12-05", "2026-12-01")
    with pytest.raises(UserError):
        env["sale.rental_allocation"].rental_history(kit.ids[0], "soon", "2026-12-01")


def test_access_rights(env: Any) -> None:
    kit = rental_product(env, units=1)
    seller = env.with_user(_user(env, "rent-own@rental.test", ["sale.group_sale_user"]).ids[0])
    mine = rental_order(seller, kit, at(1), at(2))
    mine.action_confirm()
    rental_line(mine).action_pickup()
    assert seller["sale.rental_allocation"].search([("product_id", "=", kit.ids[0])])
    assert seller["sale.rental_pricing"].search([("product_id", "=", kit.ids[0])])
    with pytest.raises(AccessError):
        seller["sale.rental_pricing"].create({"product_id": kit.ids[0], "price": 1})
    with pytest.raises(AccessError):
        seller["sale.rental_allocation"].create(
            {
                "line_id": rental_line(mine).ids[0],
                "product_id": kit.ids[0],
                "unit": 1,
                "date_start": at(5),
                "date_end": at(6),
            }
        )
    with pytest.raises(AccessError):
        seller["sale.rental_allocation"].search([], limit=1).unlink()
    manager = env.with_user(_user(env, "rent-manager@rental.test", ["sale.group_sale_manager"]).ids[0])
    assert manager["sale.rental_pricing"].create({"product_id": kit.ids[0], "price": 5, "unit": "hour"})
    # another salesperson cannot pick up someone else's rental
    other = env.with_user(_user(env, "rent-other@rental.test", ["sale.group_sale_user"]).ids[0])
    theirs = rental_order(env, kit, at(10), at(11))
    theirs.action_confirm()
    with pytest.raises(AccessError):
        other["sale.quotation_line"].browse(rental_line(theirs).ids).action_pickup()
    plain = env.with_user(_user(env, "rent-plain@rental.test", ["core.group_user"]).ids[0])
    with pytest.raises(AccessError):
        plain["sale.rental_allocation"].search([])
    with pytest.raises(AccessError):
        plain["sale.rental_allocation"].availability(kit.ids[0], at(1).isoformat(), at(2).isoformat())


def test_company_isolation(env: Any) -> None:
    eu = eu_env(env)
    kit = rental_product(env, units=2)
    eu_rule = eu["sale.rental_pricing"].create(
        {
            "product_id": kit.ids[0],
            "price": 7,
            "company_id": eu.company.ids[0],
            "currency_id": eu.company.currency_id.ids[0],
        }
    )
    eu_order = rental_order(eu, kit, at(1), at(2))
    eu_order.action_confirm()
    assert rental_line(eu_order).price_unit == 7  # the EU company's own rule
    us_order = rental_order(env, kit, at(1), at(2))
    assert rental_line(us_order).price_unit == 100  # not the EU rule
    us = env.with_user(
        _user(env, "rent-us@rental.test", ["sale.group_sale_manager"], env.ref("core.main_company")).ids[0]
    )
    assert not us["sale.rental_allocation"].search([("order_id", "=", eu_order.ids[0])])
    assert not us["sale.rental_pricing"].search([("id", "=", eu_rule.ids[0])])
    us_order.action_confirm()  # one unit left for the US company
    assert (
        us["sale.rental_allocation"].availability(kit.ids[0], at(1).isoformat(), at(2).isoformat())["free_for_period"]
        == 0
    )


def test_demo_scenario(system_env: Any) -> None:
    from modules.sale_rental.demo import seed_rentals

    now = dt.datetime(2026, 11, 20, 10, tzinfo=dt.UTC)
    assert seed_rentals(system_env, now) == 4
    assert seed_rentals(system_env, now) == 0
    orders = system_env["sale.quotation"].search([("client_reference", "like", "DEMO-RENT-")], order="client_reference")
    assert [o.rental_status for o in orders] == ["returned", "pickedup", "late", "reserved"]
    first = orders[0]
    assert _charges(first, "late_fee").price_unit == 2 * 75  # 5 hours late at 15/hour, two projectors
    assert first.invoice_count == 1 and first.invoice_status == "invoiced"
    assert rental_line(orders[3]).rental_price_detail == "1 x 1 week + 2 x 1 day"
