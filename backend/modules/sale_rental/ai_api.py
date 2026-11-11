"""HTTP API of the rental AI features (``/api/v1/sale_rental/ai``): a product's demand forecast and the usage alerts.
The optional narratives run through the generic ``/api/v1/ai/features/<name>/run`` endpoint."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from modules.sale_rental import ai_features as AI
from tessera.ai.errors import AIError
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError
from tessera.i18n import _

router = register_router(APIRouter(prefix="/sale_rental/ai", tags=["sale_rental", "ai"]))


def _error(exc: AIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status, content={"error": type(exc).__name__, "message": exc.message, **exc.context}
    )


@router.get("/products/{product_id}/forecast", response_model=None)
def forecast(
    product_id: int, weeks: int = Query(4, ge=1, le=AI.MAX_WEEKS), ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any] | JSONResponse:
    """Units out per day and week for the next weeks, with what is booked (404 ``feature_disabled`` when off)."""
    product = ctx.env["product.product"].browse([product_id])
    if not product.exists():
        raise MissingError(_("Product not found"))
    try:
        return AI.forecast_view(ctx.env, product, weeks)
    except AIError as exc:
        return _error(exc)


@router.get("/alerts", response_model=None)
def alerts(
    product_id: int | None = None, order_id: int | None = None, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any] | JSONResponse:
    """Open usage alerts about a product or an order (its rentals and its customer)."""
    try:
        return AI.alerts_view(ctx.env, product_id=product_id, order_id=order_id)
    except AIError as exc:
        return _error(exc)


@router.post("/alerts/{alert_id}/dismiss", response_model=None)
def dismiss(alert_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    try:
        AI.ensure_enabled(ctx.env, AI.ANOMALY)
    except AIError as exc:
        return _error(exc)
    alert = ctx.env["sale.rental_anomaly"].browse([alert_id])
    if not alert.exists():
        raise MissingError(_("Alert not found"))
    alert.action_dismiss()
    return AI.alert_dict(alert)
