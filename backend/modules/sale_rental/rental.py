"""Rental services: the overdue job and the usage history used by the demand features of sub-phase 2.6."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.sale.models.quotation import INTERNAL
from tessera.i18n import _
from tessera.jobs import job
from tessera.logging import get_logger
from tessera.orm import Model

log = get_logger(__name__)


def mark_late(env: Any, now: dt.datetime | None = None) -> int:
    """Mark picked-up rentals whose return date has passed ``late``; notify each order's salesperson once."""
    moment = now or env.now()
    lines = (
        env["sale.quotation_line"]
        .elevated("overdue rentals")
        .search([("is_rental", "=", True), ("rental_status", "=", "pickedup"), ("return_date", "<", moment)])
    )
    if not lines:
        return 0
    lines.with_context(**{INTERNAL: True}).write({"rental_status": "late"})
    for order in lines.mapped("quotation_id"):
        late = lines.filtered(lambda r, o=order: r.quotation_id.ids == o.ids)
        names = ", ".join(late.mapped("product_id").mapped("display_name"))
        order._rental_log(_("Not returned on time: {products}", products=names))
        user = order.user_id or order.create_uid
        if user:
            env["core.notification"].notify(
                user.ids,
                _("Late rental: {order}", order=order.name),
                _("{customer} has not returned {products} yet", customer=order.partner_id.display_name, products=names),
                model="sale.quotation",
                res_id=order.ids[0],
            )
    log.info("rentals marked late", count=len(lines))
    return len(lines)


@job("sale_rental.mark_late", queue="default", max_attempts=1, as_system=True)
def mark_late_job(env: Any) -> dict[str, Any]:
    """Hourly: rentals not returned on time become *late* and their salesperson is notified."""
    return {"late": mark_late(env)}


def rental_history(product: Model, date_from: dt.date, date_to: dt.date, tz: str | None = None) -> list[dict[str, Any]]:
    """Daily utilisation of ``product`` from ``date_from`` to ``date_to`` (inclusive, days of ``tz``).

    Per day: ``units`` (rentable units), ``booked_units`` (units with a reservation that day), ``booked_hours``
    (reserved unit-hours), ``utilisation`` (booked hours / (units x 24), 0 to 1), ``rentals_started`` (reservations
    starting that day) and ``late_returns`` (rentals returned late that day). Reservations end at the actual
    return, so the history is what really happened for past days and what is booked for future ones."""
    from modules.sale_rental.pricing import zone

    env = product.env
    z = zone(tz or env.tz)
    p = product.elevated()
    units = int(p.rental_units or 0)
    start = dt.datetime.combine(date_from, dt.time.min, tzinfo=z).astimezone(dt.UTC)
    end = dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min, tzinfo=z).astimezone(dt.UTC)
    env.flush()
    rows = env.cr.fetchall(
        "SELECT unit, date_start, date_end, line_id FROM sale_rental_allocation"
        " WHERE product_id = :p AND date_end > :s AND date_start < :e",
        {"p": p.ids[0], "s": start, "e": end},
    )
    late = (
        env["sale.quotation_line"]
        .elevated()
        .search(
            [
                ("product_id", "=", p.ids[0]),
                ("is_rental", "=", True),
                ("rental_status", "=", "returned"),
                ("late_fee", ">", 0),
                ("returned_datetime", ">=", start),
                ("returned_datetime", "<", end),
            ]
        )
    )
    late_days = [line.returned_datetime.astimezone(z).date() for line in late]
    out: list[dict[str, Any]] = []
    day = date_from
    while day <= date_to:
        d0 = dt.datetime.combine(day, dt.time.min, tzinfo=z).astimezone(dt.UTC)
        d1 = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=z).astimezone(dt.UTC)
        booked: set[int] = set()
        seconds = 0.0
        started = 0
        for unit, a, b, _line in rows:
            lo, hi = max(a, d0), min(b, d1)
            if hi > lo:
                booked.add(int(unit))
                seconds += (hi - lo).total_seconds()
            if d0 <= a < d1:
                started += 1
        hours = round(seconds / 3600, 2)
        capacity = units * (d1 - d0).total_seconds() / 3600
        out.append(
            {
                "date": day.isoformat(),
                "units": units,
                "booked_units": len(booked),
                "booked_hours": hours,
                "utilisation": round(hours / capacity, 4) if capacity else 0.0,
                "rentals_started": started,
                "late_returns": late_days.count(day),
            }
        )
        day += dt.timedelta(days=1)
    return out
