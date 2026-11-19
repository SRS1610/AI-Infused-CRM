"""Reserved rental units (``sale.rental_allocation``) and availability.

A confirmed rental line holds one allocation per rented unit: (product, unit number, [start, end)). The database
refuses two allocations of the same unit whose periods overlap (a GiST exclusion constraint on
``tstzrange(date_start, date_end, '[)')``, needs ``btree_gist``), so no code path — and no pair of concurrent
transactions — can double-book a unit. Periods are half-open: a unit returned at 10:00 can be picked up by the next
customer at 10:00.

Choosing units (``allocate``): allocations of a product are made under a transaction-level advisory lock on the
product, so concurrent confirmations queue up and the second one sees the first one's units; a unit is free when it
has no allocation overlapping the whole period (a customer keeps the same unit from pickup to return); among the
free units the one whose previous booking ends closest to the start is taken (best fit, it keeps long free gaps
available). Should the constraint still fire (a writer that bypasses the lock), the choice is retried.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

LOCK_NAMESPACE = 72_510  # pg_advisory_xact_lock(namespace, product id)
RETRIES = 3


class RentalAllocation(Model):
    _name = "sale.rental_allocation"
    _description = "Rental reservation"
    _order = "date_start, product_id, unit, id"
    _sql_constraints = [
        ("dates", "CHECK (date_end > date_start)", "A reservation ends after it starts"),
        ("unit_positive", "CHECK (unit >= 1)", "Unit numbers start at 1"),
        (
            "no_double_booking",
            "EXCLUDE USING gist (product_id WITH =, unit WITH =, tstzrange(date_start, date_end, '[)') WITH &&)",
            "This unit is already rented out for an overlapping period",
        ),
    ]
    _indexes = [("product_period", ["product_id", "date_start", "date_end"])]

    name = fields.Char(compute="_compute_name")
    line_id = fields.Many2one(
        "sale.quotation_line", "Rental line", required=True, ondelete="cascade", index=True, readonly=True
    )
    order_id = fields.Many2one("sale.quotation", related="line_id.quotation_id", store=True, index=True, string="Order")
    partner_id = fields.Many2one("contacts.partner", related="line_id.quotation_id.partner_id", string="Customer")
    company_id = fields.Many2one("core.company", related="line_id.company_id", store=True, index=True)
    product_id = fields.Many2one("product.product", "Product", required=True, ondelete="restrict", readonly=True)
    unit = fields.Integer("Unit number", required=True, readonly=True)
    date_start = fields.Datetime("From", required=True, readonly=True)
    date_end = fields.Datetime("Until", required=True, readonly=True)
    rental_status = fields.Selection(related="line_id.rental_status", string="Status")

    def _compute_name(self) -> None:
        for rec in self:
            me = rec.elevated()
            order = me.line_id.quotation_id
            rec.name = f"{me.product_id.display_name} #{me.unit} · {order.name} · {order.partner_id.display_name}"

    # -- availability ---------------------------------------------------------------------------------

    @api.model
    def _busy(
        self, product_id: int, start: dt.datetime, end: dt.datetime, exclude_line_ids: list[int] | None = None
    ) -> dict[int, dt.datetime | None]:
        """Units with an allocation overlapping [start, end) → None, plus the free units' latest booking end
        before ``start`` (for the best-fit choice) under the key ``-unit``."""
        self.env.flush()
        rows = self.env.cr.fetchall(
            "SELECT unit, bool_or(tstzrange(date_start, date_end, '[)') && tstzrange(:s, :e, '[)')) AS busy,"
            " max(date_end) FILTER (WHERE date_end <= :s) AS last_end"
            " FROM sale_rental_allocation WHERE product_id = :p AND NOT (line_id = ANY(:ex))"
            " GROUP BY unit",
            {"p": product_id, "s": start, "e": end, "ex": list(exclude_line_ids or [])},
        )
        out: dict[int, dt.datetime | None] = {}
        for unit, busy, last_end in rows:
            out[int(unit) if busy else -int(unit)] = last_end
        return out

    @api.model
    def free_units(
        self, product: Model, start: dt.datetime, end: dt.datetime, exclude_line_ids: list[int] | None = None
    ) -> list[int]:
        """Unit numbers of ``product`` free for the whole of [start, end), best fit first."""
        units = int(product.elevated().rental_units or 0)
        info = self._busy(product.ids[0], start, end, exclude_line_ids)
        free = [u for u in range(1, units + 1) if u not in info]
        floor = dt.datetime.min.replace(tzinfo=dt.UTC)

        def fit(u: int) -> tuple[float, int]:
            last = info.get(-u) or floor
            return ((start - last).total_seconds(), u)

        return sorted(free, key=fit)

    @api.model
    def allocate(self, line: Model, start: dt.datetime, end: dt.datetime) -> Model:
        """Reserve ``line.quantity`` units of its product for [start, end) (see the module docstring)."""
        line = line.elevated()
        product = line.product_id
        needed = round(line.quantity or 0)
        records = self.elevated("rental allocation")
        if needed <= 0:
            return records.browse([])
        self.env.cr.execute("SELECT pg_advisory_xact_lock(:ns, :pid)", {"ns": LOCK_NAMESPACE, "pid": product.ids[0]})
        for _attempt in range(RETRIES):
            free = self.free_units(product, start, end, exclude_line_ids=line.ids)
            if len(free) < needed:
                raise UserError(
                    _(
                        "{product} is not available from {start} to {end}: "
                        "{free} of {units} units free, {needed} needed",
                        product=product.display_name,
                        start=_fmt(start, line),
                        end=_fmt(end, line),
                        free=len(free),
                        units=int(product.rental_units or 0),
                        needed=needed,
                    )
                )
            values = [
                {
                    "line_id": line.ids[0],
                    "product_id": product.ids[0],
                    "unit": unit,
                    "date_start": start,
                    "date_end": end,
                }
                for unit in free[:needed]
            ]
            try:
                with self.env.cr.savepoint():
                    return records.create(values)
            except ValidationError as exc:
                if exc.context.get("constraint") != "no_double_booking":
                    raise
        raise UserError(_("{product} was just booked by someone else: try again", product=product.display_name))

    @api.model
    @api.expose
    def availability(self, product_id: int, start: str, end: str, slot: str = "day") -> dict[str, Any]:
        """Free units of a product per slot (``hour`` or ``day``, in the caller's time zone) between ``start`` and
        ``end`` (ISO date-times), and how many units are free for the whole period."""
        from modules.sale_rental.pricing import zone

        self.check_access_rights("read")
        product = self.env["product.product"].browse([int(product_id)])
        product.check_access("read")
        s, e = _parse(start), _parse(end)
        if e <= s:
            raise UserError(_("The end of the period is before its start"))
        if slot not in ("hour", "day"):
            raise UserError(_("Slots are hours or days"))
        if (e - s) > dt.timedelta(days=370 if slot == "day" else 31):
            raise UserError(_("Ask for at most a year by day or a month by hour"))
        units = int(product.elevated().rental_units or 0)
        tz = zone(self.env.tz)
        self.env.flush()
        rows = self.env.cr.fetchall(
            "SELECT unit, date_start, date_end FROM sale_rental_allocation"
            " WHERE product_id = :p AND date_end > :s AND date_start < :e",
            {"p": product.ids[0], "s": s, "e": e},
        )
        slots = []
        for slot_start, slot_end in _slots(s, e, slot, tz):
            booked = {int(u) for u, a, b in rows if a < slot_end and b > slot_start}
            slots.append(
                {
                    "start": slot_start.isoformat(),
                    "end": slot_end.isoformat(),
                    "booked": len(booked),
                    "free": max(0, units - len(booked)),
                }
            )
        return {
            "product_id": product.ids[0],
            "units": units,
            "free_for_period": len(self.free_units(product, s, e)),
            "slots": slots,
        }

    @api.model
    @api.expose
    def rental_history(self, product_id: int, date_from: str, date_to: str) -> list[dict[str, Any]]:
        """Daily utilisation of a product between two dates (see ``modules.sale_rental.rental.rental_history``)."""
        from modules.sale_rental.rental import rental_history

        self.check_access_rights("read")
        product = self.env["product.product"].browse([int(product_id)])
        product.check_access("read")
        try:
            d0, d1 = dt.date.fromisoformat(str(date_from)[:10]), dt.date.fromisoformat(str(date_to)[:10])
        except ValueError as exc:
            raise UserError(_("Use ISO 8601 dates")) from exc
        if d1 < d0 or (d1 - d0).days > 731:
            raise UserError(_("Ask for at most two years, the end after the start"))
        return rental_history(product, d0, d1)


def _parse(value: str | dt.datetime) -> dt.datetime:
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise UserError(_("Use ISO 8601 date-times")) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _slots(start: dt.datetime, end: dt.datetime, slot: str, tz: dt.tzinfo) -> list[tuple[dt.datetime, dt.datetime]]:
    """Consecutive hours or local days covering [start, end)."""
    out: list[tuple[dt.datetime, dt.datetime]] = []
    if slot == "hour":
        cur = start.replace(minute=0, second=0, microsecond=0)
        while cur < end:
            nxt = cur + dt.timedelta(hours=1)
            out.append((max(cur, start), min(nxt, end)))
            cur = nxt
        return out
    local = start.astimezone(tz)
    day = local.date()
    while True:
        day_start = dt.datetime.combine(day, dt.time.min, tzinfo=tz).astimezone(dt.UTC)
        day_end = dt.datetime.combine(day + dt.timedelta(days=1), dt.time.min, tzinfo=tz).astimezone(dt.UTC)
        if day_start >= end:
            break
        out.append((max(day_start, start), min(day_end, end)))
        day += dt.timedelta(days=1)
    return out


def _fmt(when: dt.datetime, line: Model) -> str:
    from modules.sale_rental.pricing import zone

    return when.astimezone(zone(line._rental_tz())).strftime("%Y-%m-%d %H:%M")
