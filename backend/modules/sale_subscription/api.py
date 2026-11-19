"""Customer-facing subscription endpoints: list, status, next invoice, recent invoices and closing (when the
plan allows it). Access is the order's access token (the link in the quotation email) or a signed-in user
of the customer's company (portal users) or with read access (internal users)."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Body, Depends

from tessera.api.deps import RequestContext, authenticated_any, request_context
from tessera.api.middleware import limiter
from tessera.api.registry import register_router
from tessera.exceptions import AccessDenied, AccessError, MissingError, UserError
from tessera.i18n import _
from tessera.runtime import current_remote_ip

router = register_router(APIRouter(prefix="/sale/subscriptions", tags=["sale_subscription"]))
VISIBLE_STATES = ("progress", "paused", "closed", "churned")


def _own_partner(ctx: RequestContext) -> Any:
    partner = ctx.env.user.elevated().partner_id
    return partner.commercial_partner_id if partner else partner


def _subscription(ctx: RequestContext, sub_id: int, token: str | None) -> Any:
    sub = ctx.env["sale.quotation"].elevated("customer subscription").browse([sub_id]).exists()
    if not sub or not sub.is_subscription or sub.state != "accepted" or sub.upsell_of_id:
        raise MissingError(_("Subscription not found"))
    if token:
        if not limiter.allow(f"subscription-token:{current_remote_ip() or '?'}", 60):
            raise UserError(_("Too many attempts; try again in a minute"))
        expected = sub.access_token or ""
        if not expected or not hmac.compare_digest(expected, token):
            raise AccessDenied(_("Invalid or expired link"))
        return sub
    if not ctx.authenticated:
        raise AccessDenied(_("Sign in or use the link from your email"))
    partner = _own_partner(ctx)
    if partner and partner.ids == sub.commercial_partner_id.ids:
        return sub
    try:
        ctx.env["sale.quotation"].browse([sub_id]).check_access("read")
    except AccessError as exc:
        raise MissingError(_("Subscription not found")) from exc
    if ctx.env.user.elevated().share:
        raise MissingError(_("Subscription not found"))
    return sub


def subscription_payload(sub: Any) -> dict[str, Any]:
    plan = sub.plan_id
    state_labels = dict(sub._fields["subscription_state"].selection)
    unit_labels = dict(plan._fields["billing_period_unit"].selection) if plan else {}
    invoices = (
        sub.env["account.move"]
        .elevated()
        .search(
            [("subscription_id", "=", sub.ids[0]), ("state", "=", "posted")],
            order="invoice_date desc, id desc",
            limit=12,
        )
    )
    can_close = bool(plan and plan.user_closable and sub.subscription_state in ("progress", "paused"))
    reasons = sub.env["sale.subscription_close_reason"].elevated().search([("customer_visible", "=", True)])
    return {
        "id": sub.ids[0],
        "name": sub.name,
        "state": sub.subscription_state,
        "state_label": str(state_labels.get(sub.subscription_state, sub.subscription_state)),
        "plan": plan.name if plan else None,
        "billing_period": {
            "value": plan.billing_period_value if plan else None,
            "unit": plan.billing_period_unit if plan else None,
            "unit_label": str(unit_labels.get(plan.billing_period_unit, "")) if plan else None,
        },
        "start_date": sub.start_date.isoformat() if sub.start_date else None,
        "next_invoice_date": (
            sub.next_invoice_date.isoformat()
            if sub.next_invoice_date
            and sub.subscription_state == "progress"
            and (not sub.end_date or sub.next_invoice_date <= sub.end_date)
            else None
        ),
        "end_date": sub.end_date.isoformat() if sub.end_date else None,
        "closing": bool(sub.close_reason_id and sub.subscription_state in ("progress", "paused")),
        "currency": sub.currency_id.name,
        "recurring_total": sub.recurring_total,
        "recurring_total_taxed": round(sum(float(line.price_total or 0) for line in sub._recurring_lines()), 2),
        "payment_method": sub.payment_token_id.display if sub.payment_token_id else None,
        "can_close": can_close and not sub.close_reason_id,
        "close_reasons": [{"id": r.ids[0], "name": r.name} for r in reasons] if can_close else [],
        "lines": [
            {
                "id": line.ids[0],
                "name": line.name,
                "quantity": line.quantity,
                "price_unit": line.price_unit,
                "discount": line.discount,
                "price_subtotal": line.price_subtotal,
            }
            for line in sub._recurring_lines()
        ],
        "invoices": [
            {
                "id": move.ids[0],
                "name": move.name,
                "move_type": move.move_type,
                "invoice_date": move.invoice_date.isoformat() if move.invoice_date else None,
                "period_start": move.subscription_period_start.isoformat() if move.subscription_period_start else None,
                "period_end": move.subscription_period_end.isoformat() if move.subscription_period_end else None,
                "amount_total": move.amount_total,
                "payment_state": move.payment_state,
                "url": move.portal_url(),
            }
            for move in invoices
        ],
    }


@router.get("")
def list_subscriptions(ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    """The signed-in customer's subscriptions."""
    partner = _own_partner(ctx)
    if not partner:
        return {"subscriptions": []}
    subs = (
        ctx.env["sale.quotation"]
        .elevated("customer subscriptions")
        .search(
            [
                ("commercial_partner_id", "=", partner.ids[0]),
                ("is_subscription", "=", True),
                ("state", "=", "accepted"),
                ("subscription_state", "in", list(VISIBLE_STATES)),
            ],
            order="start_date desc, id desc",
            limit=100,
        )
    )
    return {
        "subscriptions": [
            {
                "id": s.ids[0],
                "name": s.name,
                "plan": s.plan_id.name if s.plan_id else None,
                "state": s.subscription_state,
                "next_invoice_date": (
                    s.next_invoice_date.isoformat()
                    if s.next_invoice_date and s.subscription_state == "progress"
                    else None
                ),
                "recurring_total": s.recurring_total,
                "recurring_total_taxed": round(sum(float(line.price_total or 0) for line in s._recurring_lines()), 2),
                "currency": s.currency_id.name,
            }
            for s in subs
        ]
    }


@router.get("/{sub_id}")
def view(sub_id: int, token: str | None = None, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    return subscription_payload(_subscription(ctx, sub_id, token))


@router.post("/{sub_id}/close")
def close(
    sub_id: int,
    reason_id: int = Body(..., embed=True),
    note: str | None = Body(None, embed=True, max_length=2000),
    token: str | None = Body(None, embed=True),
    ctx: RequestContext = Depends(request_context),
) -> dict[str, Any]:
    """The customer closes the subscription at the end of the invoiced period (when the plan allows it)."""
    from modules.sale_subscription import billing

    sub = _subscription(ctx, sub_id, token)
    if not (sub.plan_id.user_closable and sub.subscription_state in ("progress", "paused")):
        raise UserError(_("This subscription cannot be closed online: contact us"))
    if sub.close_reason_id:
        raise UserError(_("This subscription is already closing"))
    reason = sub.env["sale.subscription_close_reason"].browse([int(reason_id)]).exists()
    if not reason or not reason.customer_visible:
        raise UserError(_("Choose why you close the subscription"))
    billing.close(sub, billing.today(sub.env), reason, when="period_end", note=note or _("Closed by the customer"))
    return subscription_payload(sub)
