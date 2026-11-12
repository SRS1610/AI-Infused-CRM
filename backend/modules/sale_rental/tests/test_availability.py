"""Reservations (2-f, rental half): confirmed rentals hold units, overlapping bookings beyond the product's units
are refused — by the application and by the database's exclusion constraint, also between concurrent
transactions — and touching periods are allowed."""

from __future__ import annotations

import threading
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from modules.sale_rental.tests.common import at, rental_line, rental_order, rental_product
from tessera.exceptions import UserError, ValidationError

pytestmark = pytest.mark.db


def _units(order: Any) -> list[int]:
    return sorted(rental_line(order).rental_allocation_ids.mapped("unit"))


def test_confirmation_reserves_units_and_refuses_overbooking(env: Any) -> None:
    kit = rental_product(env, units=2)
    first = rental_order(env, kit, at(1), at(5))
    assert not rental_line(first).rental_allocation_ids  # quotations reserve nothing
    first.action_confirm()
    line = rental_line(first)
    assert line.rental_status == "reserved" and first.rental_status == "reserved" and first.is_rental_order
    assert _units(first) == [1]
    alloc = line.rental_allocation_ids
    assert (alloc.date_start, alloc.date_end) == (at(1), at(5))
    assert kit.display_name in alloc.name and first.name in alloc.name
    second = rental_order(env, kit, at(3), at(8))
    second.action_confirm()
    assert _units(second) == [2]
    third = rental_order(env, kit, at(4), at(6))
    with pytest.raises(UserError, match="0 of 2 units free, 1 needed"):
        third.action_confirm()
    assert third.state == "draft" and not rental_line(third).rental_allocation_ids
    # two units at once on a period where only one is free
    pair = rental_order(env, kit, at(6), at(9), qty=2)
    with pytest.raises(UserError, match="1 of 2 units free, 2 needed"):
        pair.action_confirm()
    assert pair.state == "draft"


def test_sequential_and_touching_periods_share_a_unit(env: Any) -> None:
    kit = rental_product(env, units=1)
    morning = rental_order(env, kit, at(1, 8), at(1, 12))
    morning.action_confirm()
    afternoon = rental_order(env, kit, at(1, 12), at(1, 18))  # starts exactly when the first ends
    afternoon.action_confirm()
    later = rental_order(env, kit, at(2, 8), at(3, 8))
    later.action_confirm()
    assert _units(morning) == _units(afternoon) == _units(later) == [1]
    overlap = rental_order(env, kit, at(1, 17, minute=59), at(2, 8))
    with pytest.raises(UserError, match="not available"):
        overlap.action_confirm()


def test_best_fit_keeps_long_gaps_free(env: Any) -> None:
    kit = rental_product(env, units=2)
    rental_order(env, kit, at(1), at(3)).action_confirm()  # unit 1
    rental_order(env, kit, at(1), at(5)).action_confirm()  # unit 2
    # from day 5 both are free: unit 2 ended right before, unit 1 two days earlier → unit 2
    nxt = rental_order(env, kit, at(5), at(7))
    nxt.action_confirm()
    assert _units(nxt) == [2]
    # so unit 1 stays free for a booking starting on day 3
    gap = rental_order(env, kit, at(3), at(9))
    gap.action_confirm()
    assert _units(gap) == [1]


def test_changing_a_confirmed_line_rechecks_availability(env: Any) -> None:
    kit = rental_product(env, units=1)
    a = rental_order(env, kit, at(1), at(3))
    b = rental_order(env, kit, at(5), at(7))
    a.action_confirm()
    b.action_confirm()
    with pytest.raises(UserError, match="not available"):
        rental_line(a).write({"return_date": at(6)})
    assert rental_line(a).rental_allocation_ids.date_end == at(3)
    rental_line(a).write({"return_date": at(5)})  # touching b: fine, price follows
    assert rental_line(a).rental_allocation_ids.date_end == at(5) and rental_line(a).price_unit == 400
    with pytest.raises(UserError, match="not available"):
        rental_line(a).write({"quantity": 2})
    # a new rental line on a confirmed order reserves at once
    with pytest.raises(UserError, match="not available"):
        a.write(
            {
                "line_ids": [
                    ("create", {"product_id": kit.ids[0], "is_rental": True, "start_date": at(2), "return_date": at(3)})
                ]
            }
        )
    a.write(
        {
            "line_ids": [
                ("create", {"product_id": kit.ids[0], "is_rental": True, "start_date": at(8), "return_date": at(9)})
            ]
        }
    )
    assert len(a.line_ids.filtered(lambda r: r.is_rental).mapped("rental_allocation_ids")) == 2
    # a line that stops being a rental frees its unit
    extra = a.line_ids.filtered(lambda r: r.is_rental and r.start_date == at(8))
    extra.write({"is_rental": False})
    assert not extra.rental_allocation_ids and not extra.rental_status


def test_cancel_and_draft_release_units(env: Any) -> None:
    kit = rental_product(env, units=1)
    a = rental_order(env, kit, at(1), at(3))
    a.action_confirm()
    blocked = rental_order(env, kit, at(2), at(4))
    with pytest.raises(UserError):
        blocked.action_confirm()
    a.action_cancel()
    assert not rental_line(a).rental_allocation_ids and not rental_line(a).rental_status
    blocked.action_confirm()
    assert blocked.state == "accepted"
    a.action_draft()
    with pytest.raises(UserError, match="not available"):
        a.action_confirm()


def test_database_refuses_double_booking(env: Any) -> None:
    """The exclusion constraint holds even for writes that skip the availability check."""
    kit = rental_product(env, units=2)
    order = rental_order(env, kit, at(1), at(5))
    order.action_confirm()
    line = rental_line(order)
    allocations = env["sale.rental_allocation"].elevated()
    with pytest.raises(ValidationError, match="already rented out"):
        allocations.create(
            {"line_id": line.ids[0], "product_id": kit.ids[0], "unit": 1, "date_start": at(4), "date_end": at(6)}
        )
    allocations.create(  # touching is allowed
        {"line_id": line.ids[0], "product_id": kit.ids[0], "unit": 1, "date_start": at(5), "date_end": at(6)}
    )
    with pytest.raises(ValidationError, match="ends after it starts"):
        allocations.create(
            {"line_id": line.ids[0], "product_id": kit.ids[0], "unit": 2, "date_start": at(6), "date_end": at(6)}
        )
    with pytest.raises(IntegrityError), env.cr.savepoint():
        env.cr.execute(
            "INSERT INTO sale_rental_allocation (line_id, product_id, unit, date_start, date_end)"
            " VALUES (:l, :p, 1, :s, :e)",
            {"l": line.ids[0], "p": kit.ids[0], "s": at(2), "e": at(3)},
        )


def test_availability_api(env: Any) -> None:
    kit = rental_product(env, units=3)
    rental_order(env, kit, at(2, 12), at(4, 12), qty=2).action_confirm()
    rental_order(env, kit, at(3), at(3, 18)).action_confirm()
    allocations = env["sale.rental_allocation"]
    result = allocations.availability(kit.ids[0], at(1, 0).isoformat(), at(5, 0).isoformat())
    assert result["units"] == 3 and result["free_for_period"] == 0
    assert [(s["start"][:10], s["booked"], s["free"]) for s in result["slots"]] == [
        ("2026-12-01", 0, 3),
        ("2026-12-02", 2, 1),
        ("2026-12-03", 3, 0),
        ("2026-12-04", 2, 1),
    ]
    hours = allocations.availability(kit.ids[0], at(3, 17).isoformat(), at(3, 19).isoformat(), slot="hour")
    assert [s["free"] for s in hours["slots"]] == [0, 1] and hours["free_for_period"] == 0
    assert allocations.availability(kit.ids[0], at(5, 0).isoformat(), at(6, 0).isoformat())["free_for_period"] == 3
    with pytest.raises(UserError):
        allocations.availability(kit.ids[0], at(5).isoformat(), at(4).isoformat())
    with pytest.raises(UserError):
        allocations.availability(kit.ids[0], at(1).isoformat(), at(2).isoformat(), slot="week")
    with pytest.raises(UserError):
        allocations.availability(kit.ids[0], "yesterday", at(2).isoformat())
    with pytest.raises(UserError):
        allocations.availability(kit.ids[0], at(1, month=1).isoformat(), at(1, month=3).isoformat(), slot="hour")
    # days follow the caller's time zone
    env.user.elevated().write({"tz": "America/New_York"})
    local = (
        env["sale.rental_allocation"]
        .with_context(tz="America/New_York")
        .availability(kit.ids[0], at(2, 5).isoformat(), at(3, 5).isoformat())
    )
    assert local["slots"][0]["start"] == at(2, 5).isoformat()


def test_calendar_entries_split_by_day(env: Any) -> None:
    from modules.sale_rental.api import calendar_entries

    kit = rental_product(env, units=1)
    order = rental_order(env, kit, at(1, 12), at(3, 10))
    order.action_confirm()
    entries = calendar_entries(env, at(1, 0), at(31, 0))
    mine = [e for e in entries if e["product_id"] == kit.ids[0]]
    assert [(e["start"], e["stop"]) for e in mine] == [
        (at(1, 12).isoformat(), at(2, 0).isoformat()),
        (at(2, 0).isoformat(), at(3, 0).isoformat()),
        (at(3, 0).isoformat(), at(3, 10).isoformat()),
    ]
    assert all(e["id"] == rental_line(order).rental_allocation_ids.ids[0] for e in mine)
    assert calendar_entries(env, at(1, 0), at(31, 0), product_id=kit.ids[0]) == mine


def test_calendar_endpoint(admin) -> None:  # type: ignore[no-untyped-def]
    ok = admin.get(
        "/api/v1/sale_rental/calendar", params={"start": "2026-12-01T00:00:00Z", "end": "2026-12-08T00:00:00Z"}
    )
    assert ok.status_code == 200 and isinstance(ok.json(), list)
    too_long = admin.get(
        "/api/v1/sale_rental/calendar", params={"start": "2026-01-01T00:00:00Z", "end": "2027-06-01T00:00:00Z"}
    )
    assert too_long.status_code == 400


def test_concurrent_confirmations_cannot_double_book(registry: Any, database_url: str) -> None:
    """Two transactions confirm overlapping rentals of the last free unit at the same time: one wins."""
    from tessera.orm.environment import environment_for

    with environment_for(database_url, uid=1, su=True) as setup:
        kit = rental_product(setup, "Concurrent kit", units=1)
        partner = setup["contacts.partner"].create({"name": "Concurrent renter"})
        ids = [rental_order(setup, kit, at(10), at(12), partner=partner).ids[0] for _ in range(2)]
        product_id, partner_id = kit.ids[0], partner.ids[0]
    first_reserved = threading.Event()
    release_first = threading.Event()
    results: dict[str, Any] = {}

    def confirm(name: str, order_id: int, before_commit: threading.Event | None) -> None:
        try:
            with environment_for(database_url, uid=1, su=True) as e:
                e["sale.quotation"].browse([order_id]).action_confirm()
                if before_commit is not None:
                    first_reserved.set()
                    before_commit.wait(10)
            results[name] = "ok"
        except UserError as exc:
            results[name] = str(exc)

    one = threading.Thread(target=confirm, args=("one", ids[0], release_first))
    two = threading.Thread(target=confirm, args=("two", ids[1], None))
    try:
        one.start()
        assert first_reserved.wait(20)
        two.start()  # blocks on the product until the first transaction ends
        two.join(1.0)
        assert two.is_alive()
        release_first.set()
        one.join(20)
        two.join(20)
        assert results["one"] == "ok"
        assert "not available" in results["two"]
    finally:
        release_first.set()
        one.join(20)
        two.join(20)
        with environment_for(database_url, uid=1, su=True) as cleanup:
            orders = cleanup["sale.quotation"].browse(ids)
            orders.mapped("line_ids").mapped("rental_allocation_ids").elevated().unlink()
            orders.with_context(_sale_internal=True).write({"state": "cancelled"})
            orders.unlink()
            cleanup["product.product"].browse([product_id]).unlink()
            # rental_product() archived the demo promotions in this committed transaction: restore them
            cleanup["sale.promotion"].elevated().with_context(active_test=False).search([]).write({"active": True})
            cleanup["contacts.partner"].browse([partner_id]).unlink()


def test_exclusion_constraint_between_connections(registry: Any, database_url: str) -> None:
    """Without the application's lock, the database alone refuses the second of two concurrent bookings."""
    from tessera.db.engine import Cursor, get_engine
    from tessera.orm.environment import environment_for

    with environment_for(database_url, uid=1, su=True) as setup:
        kit = rental_product(setup, "Raw kit", units=1)
        order = rental_order(setup, kit, at(10), at(12))
        line_id, product_id, order_id = rental_line(order).ids[0], kit.ids[0], order.ids[0]
    sql = (
        "INSERT INTO sale_rental_allocation (line_id, product_id, unit, date_start, date_end)"
        " VALUES (:l, :p, 1, :s, :e)"
    )
    a, b = Cursor(get_engine(database_url)), Cursor(get_engine(database_url))
    outcome: dict[str, Any] = {}

    def second() -> None:
        try:
            b.execute(sql, {"l": line_id, "p": product_id, "s": at(11), "e": at(13)})
            outcome["b"] = "inserted"
        except Exception as exc:  # the driver's exclusion violation
            outcome["b"] = type(getattr(exc, "orig", exc)).__name__
        finally:
            b.rollback()

    try:
        a.execute(sql, {"l": line_id, "p": product_id, "s": at(10), "e": at(12)})
        thread = threading.Thread(target=second)
        thread.start()
        thread.join(1.0)
        assert thread.is_alive()  # waits for the first transaction
        a.commit()
        thread.join(20)
        assert outcome["b"] == "ExclusionViolation"
    finally:
        a.rollback()
        a.close()
        b.close()
        with environment_for(database_url, uid=1, su=True) as cleanup:
            cleanup.cr.execute("DELETE FROM sale_rental_allocation WHERE product_id = :p", {"p": product_id})
            cleanup["sale.quotation"].browse([order_id]).unlink()
            cleanup["product.product"].browse([product_id]).unlink()


def test_availability_ignores_company_rules(env: Any) -> None:
    """Units are physical: a reservation made in another company still blocks them."""
    from modules.account.tests.common import eu_env

    kit = rental_product(env, units=1)
    rental_order(env, kit, at(1), at(3)).action_confirm()
    eu = eu_env(env)
    eu_order = rental_order(eu, kit, at(2), at(4))
    with pytest.raises(UserError, match="not available"):
        eu_order.action_confirm()
