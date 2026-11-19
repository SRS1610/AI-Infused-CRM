"""HTTP API of the CRM insight features (``/api/v1/crm/ai``). Next-best-action runs through the generic
``/api/v1/ai/features/crm.next_best_action/run`` endpoint (its handler is registered in ai_insights)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from modules.crm import ai_insights as AI
from tessera.ai.errors import AIError
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import AccessError, MissingError
from tessera.i18n import _

router = register_router(APIRouter(prefix="/crm/ai", tags=["crm", "ai"]))


def _error(exc: AIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status, content={"error": type(exc).__name__, "message": exc.message, **exc.context}
    )


def _lead(ctx: RequestContext, lead_id: int) -> Any:
    lead = ctx.env["crm.lead"].with_context(active_test=False).browse([lead_id])
    if not lead.exists():
        raise MissingError(_("No such lead"))
    lead.check_access("read")
    return lead


def _manager(ctx: RequestContext) -> None:
    if not ctx.env.registry.security.has_groups(ctx.env, ["crm.group_crm_manager"]):
        raise AccessError(_("Only sales managers can do this"))


def _company(ctx: RequestContext) -> int:
    return int(ctx.env.company.ids[0]) if ctx.env.company else int(ctx.env.ref("core.main_company").ids[0])


@router.get("/leads/{lead_id}/insights")
def lead_insights(lead_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Stored AI score (with its top factors) and deal risk of a lead; omitted when switched off."""
    return AI.lead_insights(ctx.env, _lead(ctx, lead_id))


@router.post("/leads/{lead_id}/score")
def rescore(lead_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Recompute this lead's score and risk now (in-process)."""
    return AI.rescore_now(ctx.env, _lead(ctx, lead_id))


@router.post("/leads/{lead_id}/routing", response_model=None)
def route_one(lead_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    """Suggested assignee for one lead, as a proposal (nothing changes until it is accepted)."""
    try:
        return AI.propose_routing(ctx.env, _lead(ctx, lead_id))
    except AIError as exc:
        return _error(exc)


class RoutingIn(BaseModel):
    lead_ids: list[int] | None = Field(None, max_length=200)


@router.post("/routing", response_model=None)
def route_many(data: RoutingIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    """Bulk routing: one proposal per lead, reviewed as a list with a decision per row. Without ids,
    the unassigned open leads the user can see (up to 100)."""
    leads = ctx.env["crm.lead"]
    if data.lead_ids:
        leads = leads.browse(data.lead_ids).exists()
    else:
        leads = leads.search([("user_id", "=", None), ("is_won", "=", False)], limit=100)
    try:
        return AI.propose_routing(ctx.env, leads)
    except AIError as exc:
        return _error(exc)


@router.get("/forecast")
def latest_forecast(team_id: int | None = None, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """The latest stored forecast of the current company (and team)."""
    company = _company(ctx)
    enabled = AI.feature_enabled(ctx.env, AI.FORECAST, company)
    if not enabled:
        return {"enabled": False, "forecast": None}
    found = ctx.env["crm.ai_forecast"].search(
        [("company_id", "=", company), ("team_id", "=", team_id or None)], order="id desc", limit=1
    )
    return {"enabled": True, "forecast": AI.forecast_dict(found) if found else None}


class ForecastIn(BaseModel):
    team_id: int | None = None
    narrative: bool = False


@router.post("/forecast", status_code=202, response_model=None)
def request_forecast(data: ForecastIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    """Queue a forecast; the requester gets a notification with a link when it is ready."""
    from tessera.jobs import enqueue

    try:
        AI.ensure_enabled(ctx.env, AI.FORECAST)
    except AIError as exc:
        return _error(exc)
    ctx.env["crm.lead"].check_access_rights("read")
    job = enqueue(
        ctx.env,
        "crm.ai_forecast",
        {"team_id": data.team_id, "narrative": data.narrative},
        description="Sales forecast",
        queue="ai",
    )
    return {"job_id": job.ids[0], "status": "queued"}


@router.get("/score-model")
def score_model(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    model = AI.active_model(ctx.env, _company(ctx))
    if model is None:
        return {"model": None, "fallback": "scoring rules"}
    return {
        "model": {
            "version": model.version,
            "algorithm": model.algorithm,
            "features": model.feature_names,
            "metrics": model.metrics,
            "trained_at": model.trained_at.isoformat() if model.trained_at else None,
        }
    }


@router.post("/score-model/train", status_code=202, response_model=None)
def train(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    """Managers: retrain the company's scoring model now (background, notification when done)."""
    from tessera.jobs import enqueue

    _manager(ctx)
    try:
        AI.ensure_enabled(ctx.env, AI.SCORE)
    except AIError as exc:
        return _error(exc)
    job = enqueue(
        ctx.env,
        "crm.ai_train_score_models",
        {"company_ids": [_company(ctx)], "notify_user_id": ctx.env.uid},
        description="Train lead scoring model",
        queue="ai",
    )
    return {"job_id": job.ids[0], "status": "queued"}


@router.post("/deal-risk/run", status_code=202, response_model=None)
def run_deal_risk(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    """Managers: run the deal risk check now (background, notification when done)."""
    from tessera.jobs import enqueue

    _manager(ctx)
    try:
        AI.ensure_enabled(ctx.env, AI.RISK)
    except AIError as exc:
        return _error(exc)
    job = enqueue(
        ctx.env,
        "crm.ai_deal_risk",
        {"company_ids": [_company(ctx)], "notify_user_id": ctx.env.uid},
        description="Deal risk check",
        queue="ai",
    )
    return {"job_id": job.ids[0], "status": "queued"}
