"""Helpers for rental tests."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.sale.tests.common import customer, no_promotions
from tessera.orm import Model

UTC = dt.UTC


def at(day: int, hour: int = 9, month: int = 12, year: int = 2026, minute: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, minute, tzinfo=UTC)


def rental_product(
    env: Any,
    name: str = "Rental kit",
    *,
    units: int = 2,
    prices: list[tuple[int, str, float]] | None = None,
    **vals: Any,
) -> Model:
    no_promotions(env)
    rules = prices if prices is not None else [(1, "day", 100.0), (1, "week", 500.0)]
    return env["product.product"].create(
        {
            "name": name,
            "type": "goods",
            "list_price": 50.0,
            "rent_ok": True,
            "rental_units": units,
            "taxes_id": [("set", [])],
            "rental_pricing_ids": [("create", {"duration": d, "unit": u, "price": p}) for d, u, p in rules],
            **vals,
        }
    )


def rental_order(
    env: Any,
    product: Model,
    start: dt.datetime,
    end: dt.datetime,
    qty: float = 1,
    partner: Model | None = None,
    **vals: Any,
) -> Model:
    return env["sale.quotation"].create(
        {
            "partner_id": (partner or customer(env, "Renter")).ids[0],
            "line_ids": [
                (
                    "create",
                    {
                        "product_id": product.ids[0],
                        "quantity": qty,
                        "is_rental": True,
                        "start_date": start,
                        "return_date": end,
                    },
                )
            ],
            **vals,
        }
    )


def rental_line(order: Model) -> Model:
    return order.line_ids.filtered(lambda r: r.is_rental)
