"""Rental usage alerts (sub-phase 2.6, ``sale_rental.usage_anomaly``): flags raised by the daily check — a customer who
keeps returning late, an unusually long booking, utilisation far above or below the forecast. A flag is a notification:
nothing else changes; people dismiss the flags they have dealt with. See ``modules.sale_rental.ai_features``."""

from __future__ import annotations

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields

KINDS = [
    ("late_returns", "Repeated late returns"),
    ("long_booking", "Unusually long booking"),
    ("utilisation_spike", "Utilisation spike"),
    ("utilisation_drop", "Utilisation drop"),
]


class RentalAnomaly(Model):
    _name = "sale.rental_anomaly"
    _description = "Rental usage alert (AI)"
    _order = "detected_on desc, id desc"
    _studio = False

    name = fields.Char(compute="_compute_summary")
    summary = fields.Char("Alert", compute="_compute_summary")
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", index=True)
    kind = fields.Selection(KINDS, required=True, index=True)
    key = fields.Char(required=True, index=True, help="What the alert is about (one open alert per key)")
    product_id = fields.Many2one("product.product", "Product", ondelete="cascade", index=True)
    partner_id = fields.Many2one("contacts.partner", "Customer", ondelete="cascade", index=True)
    line_id = fields.Many2one("sale.quotation_line", "Rental", ondelete="set_null")
    order_id = fields.Many2one("sale.quotation", "Order", ondelete="set_null", index=True)
    user_id = fields.Many2one("core.user", "Notified", ondelete="set_null")
    score = fields.Float(digits=(10, 3), help="How unusual: the late share, the z-score or the relative gap")
    details = fields.Json(help="The figures behind the alert")
    detected_on = fields.Date(required=True, index=True)
    state = fields.Selection([("open", "Open"), ("dismissed", "Dismissed")], default="open", required=True, index=True)

    def _compute_summary(self) -> None:
        for rec in self:
            rec.summary = rec.describe()
            rec.name = rec.summary

    def describe(self) -> str:
        me = self.elevated()
        d = me.details or {}
        product = me.product_id.display_name if me.product_id else ""
        customer = me.partner_id.display_name if me.partner_id else ""
        if me.kind == "late_returns":
            text = _(
                "{customer} returned {late} of their last {rentals} rentals late (up to {hours} h)",
                customer=customer, late=d.get("late", 0), rentals=d.get("rentals", 0), hours=d.get("max_hours_late", 0),
            )  # fmt: skip
        elif me.kind == "long_booking":
            text = _(
                "{product} booked for {days} days by {customer}; usually {usual} days",
                product=product, days=d.get("days", 0), customer=customer, usual=d.get("usual_days", 0),
            )  # fmt: skip
        elif me.kind == "utilisation_spike":
            text = _(
                "{product}: {actual} unit-days out in the last 7 days, {expected} expected",
                product=product, actual=d.get("actual", 0), expected=d.get("expected", 0),
            )  # fmt: skip
        else:
            text = _(
                "{product}: only {actual} unit-days out in the last 7 days, {expected} expected",
                product=product, actual=d.get("actual", 0), expected=d.get("expected", 0),
            )  # fmt: skip
        return str(text)

    @api.expose
    def action_dismiss(self) -> bool:
        """Mark the alerts as dealt with (they are not raised again for the same subject while still true)."""
        self.check_access("read")
        if not self.env.user.has_group("sale.group_sale_user"):
            raise UserError(_("Only salespeople can dismiss rental alerts"))
        self.elevated("rental alert dismissed").write({"state": "dismissed"})
        return True
