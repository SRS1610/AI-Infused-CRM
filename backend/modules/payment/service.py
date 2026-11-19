"""Online payment flows: invoice checkout, provider events, saved-method charges, refunds.

``process_event`` is the single place where a provider's report changes a transaction. It first records the
event under ``UNIQUE (provider_id, event_id)``: a replayed notification is a no-op (2-j). A transaction that
becomes *done* gets an ``account.payment`` in the provider's journal, matched with its invoices through
``register_payments`` (the invoices end *Paid*, or *In payment* when the journal waits for bank matching).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Any

from modules.payment.providers import ProviderError, ProviderEvent
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.logging import get_logger
from tessera.orm import Model

log = get_logger(__name__)
FINAL = ("done", "cancel", "error")


def _new_reference(env: Any, base: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in "-/" else "-" for ch in base)[:40] or "TX"
    for _attempt in range(10):
        reference = f"{clean}-{secrets.token_hex(3).upper()}"
        if not env["payment.transaction"].elevated().search_count([("reference", "=", reference)]):
            return reference
    raise UserError(_("Could not allocate a transaction reference"))  # pragma: no cover


def payable_invoices(invoices: Model) -> Model:
    return invoices.filtered(
        lambda m: (
            m.state == "posted"
            and m.move_type == "out_invoice"
            and m.payment_state in ("not_paid", "partial")
            and m.amount_residual > 0
        )
    )


def create_invoice_transaction(
    invoices: Model, provider: Model | None = None, *, tokenize: bool = False, operation: str = "online_redirect"
) -> Model:
    """A draft transaction for what is due on ``invoices`` (one company, one currency, one customer)."""
    env = invoices.env
    invoices = invoices.elevated("online payment")
    open_ = payable_invoices(invoices)
    if not open_ or len(open_) != len(invoices):
        raise UserError(_("These invoices cannot be paid online (already paid, draft or not invoices)"))
    if len(set(open_.mapped("company_id").ids)) != 1 or len(set(open_.mapped("currency_id").ids)) != 1:
        raise UserError(_("Invoices of different companies or currencies are paid separately"))
    if len(set(open_.mapped("commercial_partner_id").ids)) != 1:
        raise UserError(_("Invoices of different customers are paid separately"))
    company = open_[0].company_id
    if provider is None:
        provider = env["payment.provider"]._available(company)[:1]
    if not provider:
        raise UserError(_("Online payment is not available"))
    provider = provider.elevated()
    if provider.state == "disabled" or provider.company_id.ids != company.ids or not provider.journal_id:
        raise UserError(_("This payment provider cannot be used for these invoices"))
    amount = float(open_[0].currency_id.round(sum(float(m.amount_residual) for m in open_)))
    return (
        env["payment.transaction"]
        .elevated("online payment")
        .create(
            {
                "reference": _new_reference(
                    env, open_[0].name if len(open_) == 1 else f"{open_[0].name}+{len(open_) - 1}"
                ),
                "provider_id": provider.ids[0],
                "operation": operation,
                "amount": amount,
                "currency_id": open_[0].currency_id.ids[0],
                "partner_id": open_[0].commercial_partner_id.ids[0],
                "invoice_ids": [("set", open_.ids)],
                "tokenize": bool(tokenize and provider.allow_tokenization),
            }
        )
    )


def start_checkout(tx: Model, return_url: str, cancel_url: str | None = None) -> str:
    """Send the transaction to its provider; returns where to redirect the customer."""
    tx = tx.elevated("online payment")
    if tx.state != "draft":
        raise UserError(_("This payment has already been started"))
    adapter = tx.provider_id._adapter()
    try:
        result = adapter.create_checkout(tx, return_url, cancel_url or return_url)
    except ProviderError as exc:
        tx._set_state("error", str(exc))
        raise UserError(_("The payment could not be started: {error}", error=str(exc))) from exc
    tx.write({"provider_reference": result.provider_reference})
    tx._set_state("pending")
    for event in result.events:
        process_event(tx.provider_id, event)
    return result.redirect_url


def _payload_hash(event: ProviderEvent) -> str:
    raw = json.dumps(
        event.raw or {"id": event.event_id, "type": event.event_type, "state": event.state}, sort_keys=True, default=str
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _find_transaction(provider: Model, event: ProviderEvent) -> Model:
    txs = provider.env["payment.transaction"].elevated()
    if event.reference:
        tx = txs.search([("reference", "=", event.reference), ("provider_id", "=", provider.ids[0])], limit=1)
        if tx:
            return tx
    if event.provider_reference:
        return txs.search(
            [("provider_reference", "=", event.provider_reference), ("provider_id", "=", provider.ids[0])], limit=1
        )
    return txs.browse([])


def process_event(provider: Model, event: ProviderEvent) -> str:
    """Apply one provider event; returns ``processed``, ``duplicate``, ``ignored`` or ``unknown``."""
    provider = provider.elevated("payment event")
    env = provider.env
    events = env["payment.event"].elevated()
    if events.search_count([("provider_id", "=", provider.ids[0]), ("event_id", "=", event.event_id)]):
        return "duplicate"
    tx = _find_transaction(provider, event)
    try:
        with env.cr.savepoint():
            record = events.create(
                {
                    "provider_id": provider.ids[0],
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "payload_hash": _payload_hash(event),
                    "transaction_id": tx.ids[0] if tx else None,
                    "state": event.state,
                    "processed_at": env.now(),
                }
            )
    except Exception:
        # a concurrent delivery of the same notification won the race (UNIQUE (provider_id, event_id))
        if events.search_count([("provider_id", "=", provider.ids[0]), ("event_id", "=", event.event_id)]):
            return "duplicate"
        raise
    if not tx:
        record.write({"result": "unknown"})
        return "unknown"
    result = _apply(tx, event)
    record.write({"result": result})
    return result


def _apply(tx: Model, event: ProviderEvent) -> str:
    if event.state == "ignored":
        return "ignored"
    if event.state == "refunded":
        return "ignored"  # refunds are created by refund_transaction; notifications only confirm them
    if tx.state in FINAL:
        return "ignored"  # never go back from a final state (late or out-of-order notifications)
    if event.provider_reference and not tx.provider_reference:
        tx.write({"provider_reference": event.provider_reference})
    if event.state == "done":
        if event.amount is not None and abs(float(event.amount) - float(tx.amount)) > 0.005:
            tx._set_state("error", _("Amount mismatch: {paid} received for {due}", paid=event.amount, due=tx.amount))
            return "processed"
        if event.currency and event.currency.upper() != tx.currency_id.name:
            tx._set_state("error", _("Currency mismatch: {currency} received", currency=event.currency))
            return "processed"
        if event.token and tx.tokenize and tx.partner_id:
            token = (
                tx.env["payment.token"]
                .elevated()
                .create(
                    {
                        "provider_id": tx.provider_id.ids[0],
                        "partner_id": tx.partner_id.ids[0],
                        "provider_ref": event.token.provider_ref,
                        "display": event.token.display,
                    }
                )
            )
            tx.write({"token_id": token.ids[0]})
        tx._set_state("done")
        _book_payment(tx)
    elif event.state in ("pending", "authorized"):
        tx._set_state(event.state, event.message)
    elif event.state in ("cancel", "error"):
        default = _("Cancelled by the customer") if event.state == "cancel" else _("The payment failed")
        tx._set_state(event.state, event.message or str(default))
    return "processed"


def _book_payment(tx: Model) -> None:
    """The accounting side of a confirmed transaction: a payment matched with the invoices."""
    from modules.account.payments import register_payments

    tx = tx.elevated("payment booking")
    if tx.payment_id:
        return
    provider = tx.provider_id
    journal = provider.journal_id
    today = tx.env.today()
    open_ = payable_invoices(tx.invoice_ids)
    due = float(tx.currency_id.round(sum(float(m.amount_residual) for m in open_)))
    if open_ and abs(due - float(tx.amount)) <= 0.005:
        payments = register_payments(
            open_,
            journal=journal,
            date=today,
            amount=float(tx.amount),
            currency=tx.currency_id,
            communication=tx.reference,
            early_discount=False,
        )
        payment = payments[:1]
    else:
        # the invoices were paid otherwise meanwhile (or the amount differs): keep the money as a customer credit
        payment = (
            tx.env["account.payment"]
            .elevated()
            .create(
                {
                    "payment_type": "inbound",
                    "partner_type": "customer",
                    "partner_id": tx.partner_id.ids[0] if tx.partner_id else None,
                    "amount": float(tx.amount),
                    "currency_id": tx.currency_id.ids[0],
                    "date": today,
                    "journal_id": journal.ids[0],
                    "payment_method": "electronic",
                    "ref": tx.reference,
                }
            )
        )
        payment.action_post()
    tx.write({"payment_id": payment.ids[0]})
    log.info("online payment booked", reference=tx.reference, payment=payment.name)


def sync_transaction(tx: Model) -> Model:
    """Pull the transaction's state from its provider (the customer came back before the notification)."""
    tx = tx.elevated("payment sync")
    if tx.state not in ("pending", "authorized"):
        return tx
    try:
        events = tx.provider_id._adapter().fetch_status(tx)
    except ProviderError as exc:
        log.warning("payment status not available", reference=tx.reference, error=str(exc))
        return tx
    for event in events:
        process_event(tx.provider_id, event)
    return tx


def handle_webhook(provider: Model, headers: dict[str, str], body: bytes) -> list[str]:
    """Verify and apply a provider notification; returns the result of each event."""
    provider = provider.elevated("payment webhook")
    events = provider._adapter().handle_webhook(headers, body)
    return [process_event(provider, event) for event in events]


def charge_token(token: Model, amount: float, *, reference: str | None = None, invoices: Model | None = None) -> Model:
    """Charge a saved payment method (subscriptions, 2.5). Invoices, when given, are matched on success."""
    token = token.elevated("token charge")
    provider = token.provider_id
    if provider.state == "disabled" or not token.active:
        raise UserError(_("This saved payment method cannot be charged"))
    currency = invoices[0].currency_id if invoices else provider.company_id.currency_id
    tx = (
        token.env["payment.transaction"]
        .elevated()
        .create(
            {
                "reference": _new_reference(token.env, reference or "CHARGE"),
                "provider_id": provider.ids[0],
                "operation": "offline",
                "amount": float(amount),
                "currency_id": currency.ids[0],
                "partner_id": token.partner_id.ids[0],
                "invoice_ids": [("set", invoices.ids if invoices else [])],
                "token_id": token.ids[0],
            }
        )
    )
    tx._set_state("pending")
    try:
        event = provider._adapter().charge_token(token, float(amount), currency.name, tx.reference)
    except ProviderError as exc:
        tx._set_state("error", str(exc))
        return tx
    process_event(provider, event)
    return tx


def refund_transaction(tx: Model, amount: float | None = None) -> Model:
    """Refund (part of) a confirmed transaction: the provider pays back, an outbound payment is booked."""
    tx = tx.elevated("refund")
    if tx.state != "done" or tx.operation == "refund":
        raise UserError(_("Only confirmed payments can be refunded"))
    already = sum(
        float(r.amount) for r in tx.search([("source_transaction_id", "=", tx.ids[0]), ("state", "=", "done")])
    )
    amount = float(amount if amount is not None else float(tx.amount) - already)
    if amount <= 0 or amount > float(tx.amount) - already + 0.005:
        raise UserError(_("The refund cannot exceed what is left of the payment"))
    try:
        event = tx.provider_id._adapter().refund(tx, amount)
    except ProviderError as exc:
        raise UserError(_("The refund failed: {error}", error=str(exc))) from exc
    refund = tx.create(
        {
            "reference": _new_reference(tx.env, f"R-{tx.reference}"),
            "provider_id": tx.provider_id.ids[0],
            "operation": "refund",
            "amount": amount,
            "currency_id": tx.currency_id.ids[0],
            "partner_id": tx.partner_id.ids[0] if tx.partner_id else None,
            "source_transaction_id": tx.ids[0],
            "provider_reference": event.provider_reference,
        }
    )
    if event.state == "error":
        refund._set_state("error", event.message or str(_("The refund failed")))
        return refund
    refund._set_state("done")
    payment = (
        tx.env["account.payment"]
        .elevated()
        .create(
            {
                "payment_type": "outbound",
                "partner_type": "customer",
                "partner_id": tx.partner_id.ids[0] if tx.partner_id else None,
                "amount": amount,
                "currency_id": tx.currency_id.ids[0],
                "date": tx.env.today(),
                "journal_id": tx.provider_id.journal_id.ids[0],
                "payment_method": "electronic",
                "ref": refund.reference,
            }
        )
    )
    payment.action_post()
    refund.write({"payment_id": payment.ids[0]})
    return refund
