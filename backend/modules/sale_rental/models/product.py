"""Rental settings of products: whether a product can be rented, how many units exist, the pricing rules, the
late fees and the security deposit."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.sale_rental.pricing import UNIT_HOURS, Block, best_price, describe, wall_clock_hours
from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

DURATION_UNITS = [("hour", "Hours"), ("day", "Days"), ("week", "Weeks"), ("month", "Months (30 days)")]


class ProductRental(Model):
    _extend = "product.product"
    _sql_constraints = [
        (
            "rental_units_positive",
            "CHECK (rental_units IS NULL OR rental_units >= 0)",
            "Rentable units cannot be negative",
        ),
    ]

    rent_ok = fields.Boolean("Can be rented", help="Offered for rent on sales orders")
    rental_units = fields.Integer(
        "Rentable units",
        default=1,
        help="How many units can be out at the same time (serial numbers come with inventory)",
    )
    rental_pricing_ids = fields.One2many("sale.rental_pricing", "product_id", "Rental prices")
    extra_hourly = fields.Monetary(
        "Late fee per hour", currency_field="currency_id", help="Charged per unit for each started hour of lateness"
    )
    extra_daily = fields.Monetary(
        "Late fee per day", currency_field="currency_id", help="Charged per unit for each started day of lateness"
    )
    rental_deposit = fields.Monetary(
        "Security deposit",
        currency_field="currency_id",
        help="Per unit, invoiced with the rental and credited back when the units are returned",
    )

    def _rental_blocks(self, order: Model) -> list[Block]:
        """The pricing rules for ``order`` as optimiser blocks, prices in the order's currency. Rules of the order's
        pricelist win; else the rules without a pricelist; without any rule the sales price is a price per day."""
        self.ensure_one()
        me = self.elevated()
        order = order.elevated()
        company_id = order.company_id.ids[0]
        rules = me.rental_pricing_ids.filtered(
            lambda r: r.active and (not r.company_id or r.company_id.ids[0] == company_id)
        )
        if order.pricelist_id:
            specific = rules.filtered(lambda r: r.pricelist_id.ids == order.pricelist_id.ids)
        else:
            specific = rules.browse([])
        chosen = specific or rules.filtered(lambda r: not r.pricelist_id)
        day = order.date_order.date() if order.date_order else self.env.today()
        blocks = []
        for rule in chosen:
            price = float(rule.price or 0.0)
            currency = rule.currency_id
            if currency and order.currency_id and currency.ids != order.currency_id.ids:
                price = currency.convert(price, order.currency_id, order.company_id, day)
            blocks.append(Block(rule.ids[0], UNIT_HOURS[rule.unit] * int(rule.duration or 1), price, rule._label()))
        if not blocks:
            price = order._price_for(self, 1.0, None)
            blocks.append(Block(0, 24, price, str(_("{count} day", count=1))))
        return blocks

    def rental_price(
        self, order: Model, start: dt.datetime, end: dt.datetime, tz: str | None = None
    ) -> tuple[float, str]:
        """Price of renting one unit from ``start`` to ``end`` on ``order`` and how it is made up."""
        self.ensure_one()
        hours = wall_clock_hours(start, end, tz)
        blocks = self._rental_blocks(order)
        try:
            price, used = best_price(blocks, hours)
        except ValueError as exc:
            raise UserError(_("A rental cannot last more than five years")) from exc
        return float(order.currency_id.round(price)), describe(blocks, used)


class RentalPricing(Model):
    _name = "sale.rental_pricing"
    _description = "Rental price"
    _order = "product_id, pricelist_id, unit, duration, id"
    _sql_constraints = [
        ("duration_positive", "CHECK (duration > 0)", "A rental duration is at least 1"),
        ("price_positive", "CHECK (price >= 0)", "A rental price cannot be negative"),
    ]

    product_id = fields.Many2one("product.product", "Product", required=True, ondelete="cascade", index=True)
    duration = fields.Integer(default=1, required=True)
    unit = fields.Selection(DURATION_UNITS, default="day", required=True)
    price = fields.Monetary(currency_field="currency_id", required=True, default=0.0)
    pricelist_id = fields.Many2one(
        "product.pricelist",
        "Pricelist",
        ondelete="cascade",
        help="Only for orders with this pricelist (empty: every other order)",
    )
    currency_id = fields.Many2one(
        "core.currency",
        required=True,
        ondelete="restrict",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).elevated().currency_id.ids[0],
    )
    company_id = fields.Many2one("core.company", ondelete="cascade", help="Empty = every company")
    active = fields.Boolean(default=True)

    def _label(self) -> str:
        n = int(self.duration or 1)
        labels = {
            "hour": (_("{count} hour", count=n), _("{count} hours", count=n)),
            "day": (_("{count} day", count=n), _("{count} days", count=n)),
            "week": (_("{count} week", count=n), _("{count} weeks", count=n)),
            "month": (_("{count} month", count=n), _("{count} months", count=n)),
        }[self.unit]
        return str(labels[0] if n == 1 else labels[1])

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("pricelist_id") and not vals.get("currency_id"):
                pricelist = self.env["product.pricelist"].elevated().browse([int(vals["pricelist_id"])])
                vals["currency_id"] = pricelist.currency_id.ids[0]
        return super().create(batch)

    @api.constrains("pricelist_id", "currency_id")
    def _check_currency(self) -> None:
        for rule in self:
            pricelist = rule.pricelist_id.elevated()
            if pricelist and pricelist.currency_id.ids != rule.currency_id.ids:
                raise ValidationError(
                    _("A rental price of a pricelist is in the pricelist's currency"), field="currency_id"
                )


def rental_product_values(product: Model) -> dict[str, Any]:
    """The rental settings of ``product`` as plain values (used by the demand features of sub-phase 2.6)."""
    p = product.elevated()
    return {
        "product_id": p.ids[0],
        "name": p.display_name,
        "units": int(p.rental_units or 0),
        "extra_hourly": float(p.extra_hourly or 0.0),
        "extra_daily": float(p.extra_daily or 0.0),
        "deposit": float(p.rental_deposit or 0.0),
        "prices": [
            {"duration": r.duration, "unit": r.unit, "price": float(r.price or 0.0), "currency": r.currency_id.name}
            for r in p.rental_pricing_ids.filtered(lambda r: r.active)
        ],
    }
