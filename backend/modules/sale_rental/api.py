"""Rental API: the availability calendar's entries (one per reserved unit and day)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from fastapi import APIRouter, Depends

from modules.sale_rental.models.allocation import _parse, _slots
from modules.sale_rental.pricing import zone
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import UserError
from tessera.i18n import _

router = register_router(APIRouter(prefix="/sale_rental", tags=["sale_rental"]))


def calendar_entries(
    env: Any, start: dt.datetime, end: dt.datetime, product_id: int | None = None
) -> list[dict[str, Any]]:
    """Reserved units overlapping [start, end), split by day of the user's time zone so a rental shows on every
    day it covers."""
    domain: list[Any] = [("date_end", ">", start), ("date_start", "<", end)]
    if product_id:
        domain.append(("product_id", "=", product_id))
    allocations = env["sale.rental_allocation"].search(domain, order="product_id, unit, date_start")
    tz = zone(env.tz)
    out: list[dict[str, Any]] = []
    for rec in allocations:
        lo, hi = max(rec.date_start, start), min(rec.date_end, end)
        for day_start, day_end in _slots(lo, hi, "day", tz):
            out.append(
                {
                    "id": rec.ids[0],
                    "name": rec.name,
                    "start": day_start.isoformat(),
                    "stop": day_end.isoformat(),
                    "recurring": False,
                    "product_id": rec.product_id.ids[0],
                    "unit": rec.unit,
                }
            )
    return out


@router.get("/calendar")
def calendar(
    start: str, end: str, product_id: int | None = None, ctx: RequestContext = Depends(authenticated)
) -> list[dict[str, Any]]:
    """Calendar entries of the reserved units in [start, end) (at most a year)."""
    s, e = _parse(start), _parse(end)
    if e <= s or e - s > dt.timedelta(days=370):
        raise UserError(_("Ask for at most one year"))
    return calendar_entries(ctx.env, s, e, product_id)
