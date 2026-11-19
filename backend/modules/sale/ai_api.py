"""HTTP API of the sales AI features (``/api/v1/sale/ai``). Lines from pasted text and the optional upsell
explanation run through the generic ``/api/v1/ai/features/<name>/run`` endpoint (handlers in
``ai_quote_text`` / ``ai_upsell``). Every route answers 404 ``feature_disabled`` when its feature is switched
off for the company, before anything is computed."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from modules.sale import ai_common as AC
from modules.sale import ai_discount, ai_pricing, ai_upsell
from tessera.ai.runtime import proposal_dict
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError
from tessera.i18n import _

router = register_router(APIRouter(prefix="/sale/ai", tags=["sale", "ai"]))


def _order(ctx: RequestContext, order_id: int, feature: str) -> Any:
    AC.ensure_enabled(ctx.env, feature)
    order = ctx.env["sale.quotation"].browse([order_id])
    if not order.exists():
        raise MissingError(_("No such quotation"))
    order.check_access("read")
    return order


@router.get("/quotations/{order_id}/upsell")
def upsell(order_id: int, limit: int = 5, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Ranked products to add, with the order-history counts behind each one."""
    return ai_upsell.suggest_products(_order(ctx, order_id, ai_upsell.FEATURE), max(1, min(limit, 10)))


class ProposeLineIn(BaseModel):
    product_id: int
    quantity: float = Field(1.0, gt=0, le=1e6)
    run_id: int | None = None


@router.post("/quotations/{order_id}/upsell/propose", status_code=201)
def upsell_propose(order_id: int, data: ProposeLineIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """A proposal adding the product as a new line (nothing changes until it is accepted)."""
    order = _order(ctx, order_id, ai_upsell.FEATURE)
    return proposal_dict(ai_upsell.propose_line(order, data.product_id, data.quantity, data.run_id))


@router.post("/quotations/{order_id}/discounts")
def discounts(order_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """A discount per line inside its approved band, each as a proposal on the line."""
    order = _order(ctx, order_id, ai_discount.FEATURE)
    ai_discount.check_editable(order)
    return ai_discount.suggest(order)


@router.get("/discounts/acceptance")
def discount_acceptance(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Accepted, edited and rejected discount suggestions of the current company (sales managers)."""
    AC.ensure_enabled(ctx.env, ai_discount.FEATURE)
    return ai_discount.acceptance(ctx.env)


@router.get("/quotations/{order_id}/pricing")
def pricing(order_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Live pricing findings of the quotation and its stored flags (with their decisions)."""
    order = _order(ctx, order_id, ai_pricing.FEATURE)
    flags = ctx.env["sale.pricing_flag"].search(
        [("quotation_id", "=", order_id), ("state", "in", ["open", "approved"])]
    )
    return {
        "findings": ai_pricing.check(order),
        "flags": [ai_pricing.flag_dict(f) for f in flags],
        "ai_generated": True,
    }


@router.post("/quotations/{order_id}/pricing/check")
def pricing_check(order_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Run the check now: store the flags and notify the sales administrators of new breaches."""
    return ai_pricing.sync_flags(_order(ctx, order_id, ai_pricing.FEATURE))


@router.post("/pricing-flags/{flag_id}/approve")
def approve_flag(flag_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Approvers of the line's band (or sales administrators) approve a flagged price."""
    AC.ensure_enabled(ctx.env, ai_pricing.FEATURE)
    flag = ctx.env["sale.pricing_flag"].browse([flag_id])
    if not flag.exists():
        raise MissingError(_("No such pricing flag"))
    flag.action_approve()
    return ai_pricing.flag_dict(flag)
