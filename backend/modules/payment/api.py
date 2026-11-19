"""Payment endpoints: paying an invoice from the portal or its emailed link, and provider webhooks.

Paying needs the same access as viewing the invoice (``modules.account.api.customer_invoice``: the invoice's
access token, or a portal / internal session that can read it). Webhooks are public: their authenticity is
the provider's signature, checked by the adapter.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, Request

from modules.account.api import customer_invoice
from modules.payment.providers import ProviderError, SignatureError
from tessera.api.deps import RequestContext, request_context
from tessera.api.middleware import limiter
from tessera.api.registry import register_router
from tessera.config import get_settings
from tessera.exceptions import AccessDenied, MissingError, UserError
from tessera.i18n import _
from tessera.logging import get_logger
from tessera.runtime import current_remote_ip

log = get_logger(__name__)
router = register_router(APIRouter(prefix="/payment", tags=["payment"]))


def _providers_payload(env: Any, move: Any) -> list[dict[str, Any]]:
    providers = env["payment.provider"]._available(move.company_id)
    return [
        {
            "id": p.ids[0],
            "name": p.display_as or p.name,
            "code": p.code,
            "test_mode": p.state == "test",
            "can_save": bool(p.allow_tokenization),
        }
        for p in providers
    ]


def _transactions(env: Any, move: Any) -> list[dict[str, Any]]:
    txs = env["payment.transaction"].elevated().search([("invoice_ids", "in", move.ids)], order="id desc", limit=5)
    return [
        {"reference": tx.reference, "state": tx.state, "message": tx.state_message, "amount": tx.amount} for tx in txs
    ]


@router.get("/invoices/{move_id}")
def payment_options(
    move_id: int, token: str | None = None, ctx: RequestContext = Depends(request_context)
) -> dict[str, Any]:
    """Whether the invoice can be paid online, with which providers, and its recent payment attempts."""
    from modules.payment.service import payable_invoices

    move = customer_invoice(ctx, move_id, token)
    providers = _providers_payload(ctx.env, move) if payable_invoices(move) else []
    return {
        "invoice_id": move.ids[0],
        "payment_state": move.payment_state,
        "amount_residual": move.amount_residual,
        "currency": move.currency_id.name,
        "can_pay": bool(providers),
        "providers": providers,
        "transactions": _transactions(ctx.env, move),
    }


@router.post("/invoices/{move_id}/pay")
def pay_invoice(
    move_id: int,
    token: str | None = Body(None, embed=True),
    provider_id: int | None = Body(None, embed=True),
    save_method: bool = Body(False, embed=True),
    ctx: RequestContext = Depends(request_context),
) -> dict[str, Any]:
    """Start paying what is due on the invoice; returns the URL to send the customer to."""
    from modules.payment.service import create_invoice_transaction, start_checkout

    if not limiter.allow(f"invoice-pay:{current_remote_ip() or '?'}", 20):
        raise UserError(_("Too many attempts; try again in a minute"))
    move = customer_invoice(ctx, move_id, token)
    provider = None
    if provider_id:
        provider = ctx.env["payment.provider"]._available(move.company_id).filtered(lambda p: p.ids[0] == provider_id)
        if not provider:
            raise MissingError(_("Payment provider not found"))
    tx = create_invoice_transaction(move, provider, tokenize=save_method)
    base = get_settings().public_url.rstrip("/")
    link = f"{base}/portal/invoices/{move.ids[0]}?payment={quote(tx.reference)}"
    if token:
        link += f"&token={quote(token)}"
    redirect = start_checkout(tx, return_url=link, cancel_url=f"{link}&cancelled=1")
    return {"reference": tx.reference, "redirect_url": redirect, "state": tx.state}


@router.get("/invoices/{move_id}/status")
def transaction_status(
    move_id: int, reference: str, token: str | None = None, ctx: RequestContext = Depends(request_context)
) -> dict[str, Any]:
    """The state of a payment attempt (the return page polls it; asks the provider when still pending)."""
    from modules.payment.service import sync_transaction

    move = customer_invoice(ctx, move_id, token)
    tx = (
        ctx.env["payment.transaction"]
        .elevated()
        .search([("reference", "=", reference), ("invoice_ids", "in", move.ids)], limit=1)
    )
    if not tx:
        raise MissingError(_("Payment not found"))
    sync_transaction(tx)
    return {
        "reference": tx.reference,
        "state": tx.state,
        "message": tx.state_message,
        "payment_state": move.payment_state,
    }


@router.post("/webhook/{provider_id}")
async def webhook(provider_id: int, request: Request, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    """Provider notifications. Replaying an event is a no-op (``UNIQUE (provider_id, event_id)``)."""
    from modules.payment.service import handle_webhook

    body = await request.body()
    provider = ctx.system_env()["payment.provider"].browse([provider_id]).exists()
    if not provider or provider.state == "disabled":
        raise MissingError(_("Payment provider not found"))
    try:
        results = handle_webhook(provider, dict(request.headers), body)
    except SignatureError as exc:
        log.warning("payment webhook refused", provider=provider_id, reason=str(exc))
        raise AccessDenied(_("Invalid signature")) from exc
    except ProviderError as exc:
        raise UserError(str(exc)) from exc
    return {"received": len(results), "results": results}
