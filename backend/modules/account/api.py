"""Customer-facing invoice endpoints: the emailed link (access token, no login) or a portal / internal session
that can read the invoice. Only posted customer invoices and credit notes are exposed."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from tessera.api.deps import RequestContext, request_context
from tessera.api.middleware import limiter
from tessera.api.registry import register_router
from tessera.exceptions import AccessDenied, AccessError, MissingError, UserError
from tessera.i18n import _
from tessera.runtime import current_remote_ip

router = register_router(APIRouter(prefix="/account/invoices", tags=["account"]))


def customer_invoice(ctx: RequestContext, move_id: int, token: str | None) -> Any:
    """The invoice, elevated, after checking the token or the caller's read access."""
    move = ctx.env["account.move"].elevated("customer access").browse([move_id]).exists()
    if not move or move.state != "posted" or move.move_type not in ("out_invoice", "out_refund"):
        raise MissingError(_("Invoice not found"))
    if token:
        if not limiter.allow(f"invoice-token:{current_remote_ip() or '?'}", 60):
            raise UserError(_("Too many attempts; try again in a minute"))
        expected = move.access_token or ""
        if not expected or not hmac.compare_digest(expected, token):
            raise AccessDenied(_("Invalid or expired link"))
        return move
    if not ctx.authenticated:
        raise AccessDenied(_("Sign in or use the link from your email"))
    try:
        ctx.env["account.move"].browse([move_id]).check_access("read")
    except AccessError as exc:
        raise MissingError(_("Invoice not found")) from exc
    return move


def invoice_payload(move: Any) -> dict[str, Any]:
    company = move.company_id
    return {
        "id": move.ids[0],
        "name": move.name,
        "move_type": move.move_type,
        "state": move.state,
        "payment_state": move.payment_state,
        "invoice_date": move.invoice_date.isoformat() if move.invoice_date else None,
        "invoice_date_due": move.invoice_date_due.isoformat() if move.invoice_date_due else None,
        "customer": move.partner_id.display_name,
        "company": {"name": company.name, "email": company.email, "phone": company.phone},
        "currency": move.currency_id.name,
        "amount_untaxed": move.amount_untaxed,
        "amount_tax": move.amount_tax,
        "amount_total": move.amount_total,
        "amount_residual": move.amount_residual,
        "payment_reference": move.payment_reference or move.name,
        "narration": move.narration,
        "lines": [
            {
                "id": line.ids[0],
                "display_type": line.display_type,
                "name": line.name,
                "quantity": line.quantity,
                "uom": line.uom_id.name if line.uom_id else None,
                "price_unit": line.price_unit,
                "discount": line.discount,
                "taxes": [t.description or t.name for t in line.tax_ids],
                "price_subtotal": line.price_subtotal,
            }
            for line in move.invoice_line_ids.sorted(lambda r: (r.sequence or 0, r.ids[0]))
        ],
    }


@router.get("/{move_id}")
def view(move_id: int, token: str | None = None, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    return invoice_payload(customer_invoice(ctx, move_id, token))


@router.get("/{move_id}/pdf")
def pdf(move_id: int, token: str | None = None, ctx: RequestContext = Depends(request_context)) -> Response:
    move = customer_invoice(ctx, move_id, token)
    template = ctx.env.ref("account.report_invoice")
    content, mimetype, filename = template.with_env(move.env).render(move.ids, "pdf")
    return Response(content, media_type=mimetype, headers={"Content-Disposition": f'inline; filename="{filename}"'})
