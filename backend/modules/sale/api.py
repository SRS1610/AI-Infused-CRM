"""Customer-facing quotation endpoints. Access is either the quotation's access token (the link in the
email, no login needed) or a portal/internal session that can read the quotation."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Body, Depends
from fastapi.responses import Response

from tessera.api.deps import RequestContext, request_context
from tessera.api.middleware import limiter
from tessera.api.registry import register_router
from tessera.exceptions import AccessDenied, AccessError, MissingError, UserError
from tessera.i18n import _
from tessera.runtime import current_remote_ip

router = register_router(APIRouter(prefix="/sale/quotes", tags=["sale"]))


def _quote(ctx: RequestContext, quote_id: int, token: str | None) -> Any:
    """The quotation, elevated, after checking the token or the caller's read access."""
    sys_quote = ctx.env["sale.quotation"].elevated("customer access").browse([quote_id]).exists()
    if not sys_quote or sys_quote.state in ("draft", "cancelled"):
        raise MissingError(_("Quotation not found"))
    if token:
        if not limiter.allow(f"quote-token:{current_remote_ip() or '?'}", 60):
            raise UserError(_("Too many attempts; try again in a minute"))
        expected = sys_quote.access_token or ""
        if not expected or not hmac.compare_digest(expected, token):
            raise AccessDenied(_("Invalid or expired link"))
        return sys_quote
    if not ctx.authenticated:
        raise AccessDenied(_("Sign in or use the link from your email"))
    try:
        ctx.env["sale.quotation"].browse([quote_id]).check_access("read")
    except AccessError as exc:
        raise MissingError(_("Quotation not found")) from exc
    return sys_quote


def quote_payload(q: Any) -> dict[str, Any]:
    company = q.company_id
    return {
        "id": q.ids[0],
        "name": q.name,
        "state": q.state,
        "date_order": q.date_order.isoformat() if q.date_order else None,
        "validity_date": q.validity_date.isoformat() if q.validity_date else None,
        "customer": q.partner_id.display_name,
        "company": {"name": company.name, "email": company.email, "phone": company.phone},
        "salesperson": q.user_id.name if q.user_id else None,
        "currency": q.currency_id.name,
        "amount_untaxed": q.amount_untaxed,
        "amount_tax": q.amount_tax,
        "amount_total": q.amount_total,
        "note": q.note,
        "signed_by": q.signed_by,
        "signed_at": q.signed_at.isoformat() if q.signed_at else None,
        "lines": [
            {
                "id": line.ids[0],
                "display_type": line.display_type,
                "name": line.name,
                "quantity": line.quantity,
                "uom": line.uom_id.name if line.uom_id else None,
                "price_unit": line.price_unit,
                "discount": line.discount,
                "tax_percent": line.tax_percent,
                "price_subtotal": line.price_subtotal,
                "is_optional": line.is_optional,
            }
            for line in q.line_ids
        ],
    }


@router.get("/{quote_id}")
def view(quote_id: int, token: str | None = None, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    q = _quote(ctx, quote_id, token)
    return quote_payload(q)


@router.get("/{quote_id}/pdf")
def pdf(quote_id: int, token: str | None = None, ctx: RequestContext = Depends(request_context)) -> Response:
    q = _quote(ctx, quote_id, token)
    template = ctx.env.ref("sale.report_quotation")
    content, mimetype, filename = template.with_env(q.env).render(q.ids, "pdf")
    return Response(content, media_type=mimetype, headers={"Content-Disposition": f'inline; filename="{filename}"'})


@router.post("/{quote_id}/accept")
def accept(
    quote_id: int,
    name: str = Body(..., embed=True, max_length=200),
    email: str | None = Body(None, embed=True, max_length=200),
    token: str | None = Body(None, embed=True),
    ctx: RequestContext = Depends(request_context),
) -> dict[str, Any]:
    q = _quote(ctx, quote_id, token)
    q.accept(name, email or (ctx.env.user.email if ctx.authenticated else None), current_remote_ip())
    return quote_payload(q)


@router.post("/{quote_id}/reject")
def reject(
    quote_id: int,
    reason: str | None = Body(None, embed=True, max_length=2000),
    token: str | None = Body(None, embed=True),
    ctx: RequestContext = Depends(request_context),
) -> dict[str, Any]:
    q = _quote(ctx, quote_id, token)
    q.reject(reason)
    return quote_payload(q)
