"""Payment transactions and received provider events (ERD_PHASE_2 §5).

``payment.event`` has ``UNIQUE (provider_id, event_id)``: a notification delivered twice is recorded once and
processed once (2-j). Failed and cancelled transactions keep the provider's explanation in ``state_message``.
"""

from __future__ import annotations

from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields

TX_STATES = [
    ("draft", "Draft"),
    ("pending", "Pending"),
    ("authorized", "Authorized"),
    ("done", "Confirmed"),
    ("cancel", "Cancelled"),
    ("error", "Error"),
]


class PaymentTransaction(Model):
    _name = "payment.transaction"
    _description = "Payment transaction"
    _order = "id desc"
    _rec_name = "reference"
    _audit = True
    _sql_constraints = [("reference_uniq", "UNIQUE (reference)", "Transaction references must be unique")]

    reference = fields.Char(required=True, readonly=True, index=True, copy=False)
    provider_id = fields.Many2one("payment.provider", required=True, ondelete="restrict", index=True)
    provider_code = fields.Selection(related="provider_id.code", string="Provider type")
    company_id = fields.Many2one("core.company", related="provider_id.company_id", store=True, index=True)
    operation = fields.Selection(
        [("online_redirect", "Online payment"), ("offline", "Saved method"), ("refund", "Refund")],
        default="online_redirect",
        required=True,
        readonly=True,
    )
    amount = fields.Monetary(required=True, readonly=True)
    currency_id = fields.Many2one("core.currency", required=True, ondelete="restrict", readonly=True)
    partner_id = fields.Many2one("contacts.partner", "Customer", ondelete="restrict", index=True, readonly=True)
    invoice_ids = fields.Many2many(
        "account.move",
        "Invoices",
        relation="payment_transaction_invoice_rel",
        column1="transaction_id",
        column2="move_id",
        readonly=True,
    )
    state = fields.Selection(TX_STATES, default="draft", required=True, readonly=True, index=True, tracking=True)
    state_message = fields.Text("Provider message", readonly=True)
    provider_reference = fields.Char("Provider reference", readonly=True, index=True)
    payment_id = fields.Many2one("account.payment", "Payment", readonly=True, ondelete="set_null")
    token_id = fields.Many2one("payment.token", "Saved method", readonly=True, ondelete="set_null")
    tokenize = fields.Boolean("Save the payment method", readonly=True)
    source_transaction_id = fields.Many2one("payment.transaction", "Refund of", readonly=True, ondelete="set_null")
    last_state_change = fields.Datetime(readonly=True)
    event_ids = fields.One2many("payment.event", "transaction_id", "Events")

    def _set_state(self, state: str, message: str | None = None) -> None:
        vals: dict[str, Any] = {"state": state, "last_state_change": self.env.now()}
        if message is not None or state in ("done", "pending", "authorized"):
            vals["state_message"] = message
        self.elevated().write(vals)

    @api.expose
    def action_refund(self, amount: float | None = None) -> dict[str, Any]:
        from modules.payment.service import refund_transaction

        self.ensure_one()
        self.check_access("write")
        refund = refund_transaction(self, float(amount) if amount else None)
        return {"reference": refund.reference, "state": refund.state}

    @api.expose
    def action_sync(self) -> str:
        """Ask the provider for the current state (e.g. a missed notification)."""
        from modules.payment.service import sync_transaction

        self.ensure_one()
        return sync_transaction(self).state

    def write(self, vals: dict[str, Any]) -> bool:
        # every field is the provider's report or the checkout's own data: amount, state, invoices and references
        # change through ``modules.payment.service`` (elevated) only, never through the generic API (U-9 SEC-53)
        if not self.env.su:
            raise UserError(_("Payment transactions are updated by the payment provider only"))
        return super().write(vals)

    def unlink(self) -> bool:
        if self.filtered(lambda tx: tx.state not in ("draft", "cancel", "error")):
            raise UserError(_("Only draft, cancelled or failed transactions can be deleted"))
        return super().unlink()


class PaymentEvent(Model):
    _name = "payment.event"
    _description = "Payment provider notification"
    _order = "id desc"
    _sql_constraints = [
        ("provider_event_uniq", "UNIQUE (provider_id, event_id)", "This notification has already been received")
    ]

    provider_id = fields.Many2one("payment.provider", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="provider_id.company_id", store=True, index=True)
    event_id = fields.Char(required=True, readonly=True)
    event_type = fields.Char(readonly=True)
    payload_hash = fields.Char(readonly=True)
    transaction_id = fields.Many2one("payment.transaction", readonly=True, ondelete="set_null", index=True)
    state = fields.Char("Reported state", readonly=True)
    processed_at = fields.Datetime(readonly=True)
    result = fields.Char(readonly=True)
