"""Subscription plans, close reasons and the MRR history of subscriptions (ERD_PHASE_2 §7)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.sale_subscription.periods import UNITS
from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

EVENT_TYPES = [
    ("new", "New"),
    ("upsell", "Upsell"),
    ("downsell", "Downsell"),
    ("churn", "Churn"),
    ("reactivation", "Reactivation"),
    ("renewal", "Renewal"),
]


class SubscriptionPlan(Model):
    _name = "sale.subscription_plan"
    _description = "Subscription plan"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    billing_period_value = fields.Integer("Bill every", required=True, default=1)
    billing_period_unit = fields.Selection(UNITS, "Billing period", required=True, default="month")
    auto_close_limit = fields.Integer(
        "Close after (days unpaid)",
        default=30,
        help="A subscription whose invoice is still unpaid this many days after its due date is closed "
        "(reason: payment failure). 0 never closes automatically.",
    )
    payment_retry_days = fields.Integer(
        "Retry a failed payment every (days)",
        default=3,
        help="How often a declined saved payment method is charged again. 0 does not retry.",
    )
    user_closable = fields.Boolean(
        "Customers can close", help="Customers may close their subscription from the portal (at the period end)"
    )
    renewal_reminder_days = fields.Integer(
        "Renewal quotation (days before the end)",
        default=30,
        help="For fixed-term subscriptions, the renewal quotation is created and sent this many days before "
        "the end date. 0 creates none.",
    )
    company_id = fields.Many2one("core.company", ondelete="cascade", help="Leave empty to share the plan")
    note = fields.Text("Description", translate=True)
    subscription_count = fields.Integer("Subscriptions", compute="_compute_subscription_count")

    def _compute_subscription_count(self) -> None:
        orders = self.env["sale.quotation"]
        for plan in self:
            plan.subscription_count = orders.search_count(
                [("plan_id", "=", plan.ids[0]), ("is_subscription", "=", True), ("state", "=", "accepted")]
            )

    @api.constrains("billing_period_value", "auto_close_limit", "payment_retry_days", "renewal_reminder_days")
    def _check_values(self) -> None:
        for plan in self:
            if (plan.billing_period_value or 0) < 1:
                raise ValidationError(_("A plan bills at least every period"), field="billing_period_value")
            if min(plan.auto_close_limit or 0, plan.payment_retry_days or 0, plan.renewal_reminder_days or 0) < 0:
                raise ValidationError(_("Delays cannot be negative"), field="auto_close_limit")


class CloseReason(Model):
    _name = "sale.subscription_close_reason"
    _description = "Subscription close reason"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    customer_visible = fields.Boolean(
        "Offered to customers", default=True, help="Customers can pick this reason when they close on the portal"
    )


class SubscriptionLog(Model):
    _name = "sale.subscription_log"
    _description = "Subscription MRR change"
    _order = "event_date desc, id desc"

    subscription_id = fields.Many2one("sale.quotation", "Subscription", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", index=True)
    partner_id = fields.Many2one("contacts.partner", "Customer", ondelete="set_null", index=True)
    plan_id = fields.Many2one("sale.subscription_plan", "Plan", ondelete="set_null")
    user_id = fields.Many2one("core.user", "Salesperson", ondelete="set_null")
    event_date = fields.Date("Date", required=True, index=True)
    event_type = fields.Selection(EVENT_TYPES, "Event", required=True, index=True)
    currency_id = fields.Many2one("core.currency", required=True, ondelete="restrict")
    amount_signed = fields.Monetary("MRR change", currency_field="currency_id")
    recurring_monthly = fields.Monetary("MRR after", currency_field="currency_id")
    company_currency_id = fields.Many2one("core.currency", related="company_id.currency_id", string="Company currency")
    amount_company = fields.Monetary(
        "MRR change (company currency)", currency_field="company_currency_id", help="Converted at the event date"
    )
    reason_id = fields.Many2one("sale.subscription_close_reason", "Close reason", ondelete="set_null")
    note = fields.Char()

    @api.model
    @api.expose
    def subscription_metrics(self, date_from: str, date_to: str) -> dict[str, Any]:
        """MRR movements and churn rates of the current company between two dates (see ``metrics.py``)."""
        from modules.sale_subscription.metrics import subscription_metrics

        self.check_access_rights("read")
        company = self.env.company or self.env.ref("core.main_company")
        return subscription_metrics(company, dt.date.fromisoformat(str(date_from)), dt.date.fromisoformat(str(date_to)))
