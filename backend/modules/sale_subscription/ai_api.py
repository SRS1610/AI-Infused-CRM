"""HTTP API of the churn prediction (``/api/v1/sale_subscription/ai``). The renewal draft runs through the generic
``/api/v1/ai/features/sale_subscription.renewal_draft/run`` endpoint (its handler is in ``ai_features``)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from modules.sale_subscription import ai_features as AI
from tessera.ai.errors import AIError
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError
from tessera.i18n import _

router = register_router(APIRouter(prefix="/sale_subscription/ai", tags=["sale_subscription", "ai"]))


def _error(exc: AIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status, content={"error": type(exc).__name__, "message": exc.message, **exc.context}
    )


def _subscription(ctx: RequestContext, sub_id: int) -> Any:
    sub = ctx.env["sale.quotation"].browse([sub_id])
    if not sub.exists():
        raise MissingError(_("Subscription not found"))
    sub.check_access("read")
    return sub


@router.get("/subscriptions/{sub_id}/churn", response_model=None)
def churn(sub_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    """The stored churn score of a subscription with its top factors (404 ``feature_disabled`` when off)."""
    try:
        return AI.churn_view(ctx.env, _subscription(ctx, sub_id))
    except AIError as exc:
        return _error(exc)


@router.post("/subscriptions/{sub_id}/churn", response_model=None)
def rescore(sub_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    """Compute and store this subscription's churn score now (in-process, no provider call)."""
    try:
        return AI.rescore(ctx.env, _subscription(ctx, sub_id))
    except AIError as exc:
        return _error(exc)
