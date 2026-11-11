"""Subscriptions are sales orders (``sale.quotation``) with a plan and recurring products (ERD_PHASE_2 §7).

* ``product.product.recurring_invoice`` marks subscription products; a quotation with a plan and at least one
  such line ``is_subscription``. Its recurring lines are invoiced every period by the subscription engine
  (``billing.py``), never by the regular *Create invoice* (their quantity to invoice stays 0); other lines
  (set-up fees…) are invoiced once, with the first period.
* Confirming the order starts the subscription on its start date (``draft`` → ``progress``); a start date in
  the future, e.g. a renewal, starts on that day with the daily job.
* ``subscription_state``: draft (not started) / progress / paused / closed (replaced by its renewal) /
  churned (closed for any other reason, the churn flag).
* ``sale.subscription_log`` records every MRR movement (new, upsell, downsell, churn, reactivation, renewal);
  the sum of a subscription's movements is its current MRR (0 once closed).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.sale.models.quotation import INTERNAL
from modules.sale_subscription.periods import monthly_factor
from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

SUBSCRIPTION_STATES = [
    ("draft", "Not started"),
    ("progress", "In progress"),
    ("paused", "Paused"),
    ("closed", "Renewed"),
    ("churned", "Churned"),
]
RUNNING = ("progress", "paused")
MOVE_GROUPS = ("account.group_account_invoice", "account.group_account_readonly")


class ProductRecurring(Model):
    _extend = "product.product"

    recurring_invoice = fields.Boolean(
        "Subscription product", help="Invoiced every billing period on subscriptions (the price is per period)"
    )

    def _variant_shared_fields(self) -> list[str]:
        return [*super()._variant_shared_fields(), "recurring_invoice"]


class QuotationSubscription(Model):
    _extend = "sale.quotation"

    plan_id = fields.Many2one(
        "sale.subscription_plan", "Subscription plan", ondelete="restrict", tracking=True, index=True
    )
    is_subscription = fields.Boolean(
        "Subscription", compute="_compute_is_subscription", store=True, index=True, copy=False
    )
    subscription_state = fields.Selection(
        SUBSCRIPTION_STATES,
        "Subscription status",
        default="draft",
        readonly=True,
        tracking=True,
        copy=False,
        index=True,
    )
    is_churned = fields.Boolean("Churned", compute="_compute_is_churned", store=True, index=True)
    start_date = fields.Date("Start date", tracking=True, copy=False, help="Default: the confirmation date")
    next_invoice_date = fields.Date(
        "Next invoice", tracking=True, copy=False, index=True, help="Start of the next period to invoice"
    )
    end_date = fields.Date("End date", tracking=True, copy=False, help="Last day of a fixed term (empty: open-ended)")
    billing_anchor_date = fields.Date(
        "Billing anchor", readonly=True, copy=False, help="Periods start on this date plus whole billing periods"
    )
    recurring_total = fields.Monetary(
        "Recurring amount", currency_field="currency_id", compute="_compute_recurring", store=True,
        help="Untaxed amount of the recurring lines, per billing period",
    )  # fmt: skip
    recurring_monthly = fields.Monetary(
        "MRR", currency_field="currency_id", compute="_compute_recurring", store=True,
        help="Monthly recurring revenue: the recurring amount per month (untaxed)",
    )  # fmt: skip
    company_currency_id = fields.Many2one("core.currency", related="company_id.currency_id", string="Company currency")
    recurring_monthly_company = fields.Monetary(
        "MRR (company currency)", currency_field="company_currency_id", compute="_compute_recurring", store=True
    )
    close_reason_id = fields.Many2one(
        "sale.subscription_close_reason", "Close reason", ondelete="restrict", tracking=True, copy=False
    )
    close_note = fields.Text("Closing comment", copy=False)
    closed_on = fields.Date("Closed on", readonly=True, copy=False, help="First day without service")
    renewal_of_id = fields.Many2one(
        "sale.quotation", "Renewal of", ondelete="set_null", copy=False, index=True, readonly=True
    )
    renewal_ids = fields.One2many("sale.quotation", "renewal_of_id", "Renewals")
    upsell_of_id = fields.Many2one(
        "sale.quotation", "Changes subscription", ondelete="set_null", copy=False, index=True, readonly=True,
        help="This order changes the lines of that subscription (upsell / downgrade)",
    )  # fmt: skip
    upsell_ids = fields.One2many("sale.quotation", "upsell_of_id", "Changes")
    payment_token_id = fields.Many2one(
        "payment.token", "Saved payment method", ondelete="set_null", tracking=True, copy=False,
        help="Charged automatically when an invoice is created",
    )  # fmt: skip
    subscription_log_ids = fields.One2many("sale.subscription_log", "subscription_id", "MRR history")
    subscription_move_ids = fields.One2many(
        "account.move", "subscription_id", "Subscription invoices", groups=MOVE_GROUPS
    )
    subscription_invoice_count = fields.Integer("Subscription invoices", compute="_compute_subscription_moves")
    payment_failure_count = fields.Integer("Failed payments", default=0, readonly=True, copy=False)
    last_charge_date = fields.Date("Last payment attempt", readonly=True, copy=False)
    paused_on = fields.Date("Paused on", readonly=True, copy=False)
    pause_count = fields.Integer("Pauses", default=0, readonly=True, copy=False)

    # -- computed --------------------------------------------------------------------------------------

    @api.depends("plan_id", "line_ids.is_recurring")
    def _compute_is_subscription(self) -> None:
        for q in self:
            q.is_subscription = bool(q.plan_id) and any(line.is_recurring for line in q.line_ids)

    @api.depends("subscription_state")
    def _compute_is_churned(self) -> None:
        for q in self:
            q.is_churned = q.subscription_state == "churned"

    @api.depends(
        "plan_id.billing_period_unit",
        "plan_id.billing_period_value",
        "line_ids.price_subtotal",
        "line_ids.is_recurring",
        "line_ids.is_optional",
        "currency_id",
        "subscription_state",
    )
    def _compute_recurring(self) -> None:
        for q in self:
            plan = q.plan_id.elevated()
            lines = q._recurring_lines() if q.plan_id else []
            total = sum(float(line.price_subtotal or 0.0) for line in lines)
            monthly = total * monthly_factor(plan.billing_period_unit, plan.billing_period_value) if plan else 0.0
            if q.subscription_state in ("closed", "churned"):
                monthly = 0.0  # ended: no recurring revenue any more (the lines keep the last prices)
            q.recurring_total = total
            currency = q.currency_id.elevated()
            q.recurring_monthly = float(currency.round(monthly)) if currency else round(monthly, 2)
            q.recurring_monthly_company = q._to_company_currency(q.recurring_monthly, None)

    def _compute_subscription_moves(self) -> None:
        moves = self.env["account.move"].elevated()
        for q in self:
            q.subscription_invoice_count = (
                moves.search_count([("subscription_id", "=", q.ids[0]), ("state", "!=", "cancel")]) if q.ids else 0
            )

    def _recurring_lines(self) -> Model:
        return self.line_ids.filtered(
            lambda line: (
                line.is_recurring
                and not line.display_type
                and not line.is_optional
                and not line.is_downpayment
                and (line.quantity or 0.0) > 0
            )
        )

    def _to_company_currency(self, amount: float, day: dt.date | None) -> float:
        me = self.elevated()
        company = me.company_id
        if not amount or not me.currency_id or not company:
            return float(amount or 0.0)
        target = company.currency_id
        if me.currency_id.ids == target.ids:
            return float(amount)
        try:
            return float(me.currency_id.convert(amount, target, company, day))
        except UserError:
            return float(amount)  # no rate yet: kept unconverted rather than blocking billing

    # -- create / write / confirm ---------------------------------------------------------------------

    @api.constrains("start_date", "end_date")
    def _check_subscription_dates(self) -> None:
        for q in self:
            if q.start_date and q.end_date and q.end_date < q.start_date:
                raise ValidationError(_("The end date is before the start date"), field="end_date")

    @api.constrains("payment_token_id", "partner_id", "company_id")
    def _check_token(self) -> None:
        for q in self:
            token = q.payment_token_id.elevated()
            if not token:
                continue
            if token.partner_id.commercial_partner_id.ids != q.partner_id.elevated().commercial_partner_id.ids:
                raise ValidationError(
                    _("The saved payment method belongs to another customer"), field="payment_token_id"
                )
            if token.company_id.ids != q.company_id.ids:
                raise ValidationError(
                    _("The saved payment method belongs to another company"), field="payment_token_id"
                )

    def write(self, vals: dict[str, Any]) -> bool:
        vals = dict(vals)
        internal = bool(self.env.context.get(INTERNAL))
        if not internal and vals.get("next_invoice_date"):
            # moving the billing date by hand moves the billing grid with it
            vals.setdefault("billing_anchor_date", vals["next_invoice_date"])
        if not internal and "plan_id" in vals:
            for q in self:
                if q.subscription_state != "draft" and q.plan_id.ids != (
                    [int(vals["plan_id"])] if vals["plan_id"] else []
                ):
                    raise UserError(_("The plan of a started subscription cannot change ({name})", name=q.name))
        confirming = vals.get("state") == "accepted"
        before = {q.ids[0]: q.state for q in self} if confirming else {}
        result = super().write(vals)
        if confirming:
            for q in self:
                if before.get(q.ids[0]) != "accepted":
                    q._on_order_confirmed()
        return result

    def _check_cancellable(self) -> None:
        if self.subscription_state in RUNNING:
            raise UserError(_("Subscription {name} is running: close it instead of cancelling it", name=self.name))
        super()._check_cancellable()

    def _on_order_confirmed(self) -> None:
        from modules.sale_subscription import billing

        me = self.elevated("subscription confirmation")
        if me.upsell_of_id:
            billing.apply_upsell(me, billing.today(self.env))
        elif me.is_subscription:
            billing.confirm_subscription(me, billing.today(self.env))

    # -- actions ------------------------------------------------------------------------------------

    def _check_subscription(self, states: tuple[str, ...] | None = None) -> None:
        for q in self:
            if not q.is_subscription or q.state != "accepted":
                raise UserError(_("{name} is not a confirmed subscription", name=q.name))
            if states and q.subscription_state not in states:
                label = dict(self._fields["subscription_state"].selection)
                raise UserError(
                    _(
                        "Subscription {name} is {state}: this action is not possible",
                        name=q.name,
                        state=str(label.get(q.subscription_state, q.subscription_state)),
                    )
                )
            q.check_access("write")

    @api.expose
    def action_start(self) -> bool:
        """Confirm the subscription quotation (or start a confirmed one whose start date has come)."""
        from modules.sale_subscription import billing

        day = billing.today(self.env)
        for q in self:
            if q.state in ("draft", "sent"):
                if not q.is_subscription:
                    raise UserError(_("Choose a plan and at least one subscription product first"))
                q.action_confirm()
            elif q.state == "accepted" and q.is_subscription and q.subscription_state == "draft":
                q.check_access("write")
                start = q.start_date or day
                if start > day:
                    raise UserError(_("Subscription {name} starts on {date}", name=q.name, date=start.isoformat()))
                billing.start_subscription(q.elevated(), start)
            else:
                raise UserError(_("Subscription {name} has already started", name=q.name))
        return True

    @api.expose
    def action_create_recurring_invoices(self) -> dict[str, Any]:
        """Invoice the periods that are due now (what the daily job does), and charge them."""
        from modules.sale_subscription import billing

        self._check_subscription(("progress",))
        day = billing.today(self.env)
        moves = self.env["account.move"]
        for q in self:
            moves |= billing.invoice_due_periods(q.elevated("recurring invoicing"), day)
        return {"invoice_ids": moves.ids}

    @api.expose
    def action_upsell(self) -> dict[str, Any]:
        """A draft order changing this subscription, prefilled with its current recurring lines: change
        quantities or prices, add lines, set a quantity to 0 to stop a product, then confirm it."""
        from modules.sale_subscription import billing

        self.ensure_one()
        self._check_subscription(("progress",))
        upsell = billing.create_upsell(self)
        return {"upsell_id": upsell.ids[0], "name": upsell.name}

    @api.expose
    def action_renew(self) -> dict[str, Any]:
        """Create the renewal quotation of this fixed-term subscription (also done by the daily job)."""
        from modules.sale_subscription import billing

        self.ensure_one()
        self._check_subscription(RUNNING)
        renewal = billing.create_renewal(self.elevated("renewal"), send=False)
        return {"renewal_id": renewal.ids[0], "name": renewal.name}

    @api.expose
    def action_pause(self) -> bool:
        """Stop invoicing: periods starting while paused are not invoiced (the current one is kept)."""
        from modules.sale_subscription import billing

        self._check_subscription(("progress",))
        day = billing.today(self.env)
        for q in self:
            billing.pause(q.elevated("pause"), day)
        return True

    @api.expose
    def action_resume(self) -> bool:
        """Invoice again from the first period of the billing grid starting on or after today."""
        from modules.sale_subscription import billing

        self._check_subscription(("paused",))
        day = billing.today(self.env)
        for q in self:
            billing.resume(q.elevated("resume"), day)
        return True

    @api.expose
    def action_close(
        self,
        reason: str | None = None,
        when: str = "period_end",
        note: str | None = None,
        reason_id: int | None = None,
    ) -> bool:
        """Close the subscription (churn). ``reason``: a close reason code (``price``, ``quality``,
        ``competitor``, ``not_needed``, ``payment_failure``) or give ``reason_id``. ``when``: ``period_end`` —
        at the end of the invoiced period, nothing is credited; ``now`` — today, and the unused days of the
        invoiced period are credited."""
        from modules.sale_subscription import billing

        self._check_subscription(RUNNING)
        record = self._close_reason(reason, reason_id)
        if when not in ("now", "period_end"):
            raise UserError(_("Close the subscription now or at the end of the period"))
        day = billing.today(self.env)
        for q in self:
            billing.close(q.elevated("subscription close"), day, record, when=when, note=note)
        return True

    def _close_reason(self, reason: str | None, reason_id: int | None) -> Model:
        reasons = self.env["sale.subscription_close_reason"]
        record = reasons.browse([])
        if reason_id:
            record = reasons.browse([int(reason_id)]).exists()
        elif reason and str(reason).isdigit():
            record = reasons.browse([int(reason)]).exists()
        elif reason:
            record = self.env.ref(f"sale_subscription.close_reason_{reason}", raise_if_not_found=False) or reasons
        if not record:
            raise UserError(_("Choose why the subscription is closed"))
        return record

    @api.expose
    def action_reactivate(self) -> bool:
        """Restart a churned subscription today: a new billing period starts now."""
        from modules.sale_subscription import billing

        self._check_subscription(("churned",))
        day = billing.today(self.env)
        for q in self:
            billing.reactivate(q.elevated("reactivation"), day)
        return True

    @api.model
    @api.expose
    def action_run_recurring(self) -> dict[str, Any]:
        """Run the daily subscription job now for the current company (managers)."""
        from modules.sale_subscription import billing

        if not self.env.user.has_group("sale.group_sale_manager"):
            raise UserError(_("Only sales administrators can run the subscription job"))
        return billing.run_recurring(self.env.elevated("subscription job"), billing.today(self.env), self.env.company)


class QuotationLineSubscription(Model):
    _extend = "sale.quotation_line"

    is_recurring = fields.Boolean(
        "Recurring", compute="_compute_is_recurring", store=True, help="Invoiced every billing period"
    )
    subscription_line_id = fields.Many2one(
        "sale.quotation_line", "Subscription line", ondelete="set_null", copy=False, readonly=True,
        help="The subscription line this change or renewal line replaces",
    )  # fmt: skip

    @api.depends("product_id.recurring_invoice", "quotation_id.plan_id", "quotation_id.upsell_of_id")
    def _compute_is_recurring(self) -> None:
        for line in self:
            order = line.quotation_id
            line.is_recurring = bool(
                line.product_id
                and line.product_id.elevated().recurring_invoice
                and (order.plan_id or order.upsell_of_id)
            )

    @api.depends("is_recurring")
    def _compute_qty_to_invoice(self) -> None:
        super()._compute_qty_to_invoice()
        for line in self:
            if line.is_recurring:
                # billed every period by the subscription engine, never by the regular invoicing
                line.qty_to_invoice = 0.0
                line.untaxed_amount_to_invoice = 0.0
                line.invoice_status = "no"

    def _amount(self) -> float:
        """Per-period amount of the line in price terms (tax included when its taxes are), discount applied."""
        return float(self.quantity or 0.0) * self._price_reduced()


class MoveSubscription(Model):
    _extend = "account.move"
    _sql_constraints = [
        ("subscription_period_uniq", "UNIQUE (subscription_period_key)", "This period is already invoiced")
    ]

    subscription_id = fields.Many2one(
        "sale.quotation", "Subscription", ondelete="set_null", index=True, readonly=True, copy=False
    )
    subscription_kind = fields.Selection(
        [("period", "Billing period"), ("proration", "Mid-period change"), ("credit", "Unused days")],
        "Subscription document",
        readonly=True,
        copy=False,
    )
    subscription_period_start = fields.Date("Period from", readonly=True, copy=False)
    subscription_period_end = fields.Date("Period to", readonly=True, copy=False)
    subscription_period_days = fields.Integer("Days in the period", readonly=True, copy=False)
    subscription_period_key = fields.Char(readonly=True, copy=False, groups=("core.group_system",))

    @api.expose
    def button_cancel(self) -> bool:
        result = super().button_cancel()
        from modules.account.models.move import INTERNAL as ACCOUNT_INTERNAL

        # a cancelled period invoice frees its period (it can be invoiced again)
        cancelled = self.elevated().filtered(lambda m: m.state == "cancel" and m.subscription_period_key)
        if cancelled:
            cancelled.with_context(**{ACCOUNT_INTERNAL: True}).write({"subscription_period_key": None})
        return result
