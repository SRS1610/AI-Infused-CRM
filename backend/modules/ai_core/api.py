"""AI API: feature status, runs (blocking and streamed), proposals, feedback, administration."""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from tessera.ai import proposals as P
from tessera.ai.client import get_client
from tessera.ai.errors import AIError, AIUnavailable
from tessera.ai.runtime import HANDLERS, check_enabled, get_feature, proposal_dict, run_feature, stream_feature
from tessera.api.deps import RequestContext, admin_only, authenticated, database_url_for
from tessera.api.registry import register_router
from tessera.exceptions import AccessError, MissingError

router = register_router(APIRouter(prefix="/ai", tags=["ai"]))
_health_cache: dict[str, Any] = {"at": 0.0, "ok": False}


def _available(env: Any) -> bool:
    """Gateway health, cached for a few seconds (the UI asks on every page)."""
    if time.monotonic() - _health_cache["at"] < 10:
        return bool(_health_cache["ok"])
    try:
        get_client(env).health()
        ok = True
    except AIError:
        ok = False
    _health_cache.update(at=time.monotonic(), ok=ok)
    return ok


def _error(exc: AIError) -> JSONResponse:
    # returned (not raised) so the transaction commits and the failed run stays in the audit trail
    return JSONResponse(
        status_code=exc.http_status, content={"error": type(exc).__name__, "message": exc.message, **exc.context}
    )


class RunIn(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    res_model: str | None = None
    res_id: int | None = None


def _record(ctx: RequestContext, data: RunIn) -> Any:
    if not data.res_model:
        return None
    if data.res_model not in ctx.env.registry.models or data.res_id is None:
        raise MissingError("Unknown record")
    rec = ctx.env[data.res_model].browse([data.res_id])
    if not rec.exists():
        raise MissingError("Unknown record")
    rec.check_access("read")
    return rec


def _kwargs(ctx: RequestContext, name: str, data: RunIn, record: Any) -> dict[str, Any]:
    handler = HANDLERS.get(name)
    return handler(ctx.env, record, data.inputs) if handler else {}


@router.get("/status")
def status(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Which AI features this user can use in the current company (disabled ones are omitted)."""
    company = ctx.env.company.ids[0] if ctx.env.company else None
    features = {}
    for f in ctx.env["ai_core.feature"].elevated().search([]):
        if f.enabled_for(company):
            features[f.name] = {
                "title": f.title,
                "task_class": f.task_class,
                "interactive": f.interactive,
                "writes": f.writes,
                "outbound": f.outbound,
                "disclosure": f.disclosure,
                "risk_class": f.risk_class,
            }
    return {"available": _available(ctx.env), "features": features}


@router.post("/features/{name}/run", response_model=None)
def run(name: str, data: RunIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    record = _record(ctx, data)
    try:
        result = run_feature(ctx.env, name, data.inputs, record=record, **_kwargs(ctx, name, data, record))
    except AIError as exc:
        return _error(exc)
    return result.to_dict()


@router.post("/features/{name}/stream", response_model=None)
def stream(name: str, data: RunIn, ctx: RequestContext = Depends(authenticated)) -> StreamingResponse | JSONResponse:
    """Server-Sent Events: ``start``, ``delta``…, then ``done`` (with proposals) or ``error``. Kill
    switches are checked here, before the stream opens (404 ``feature_disabled``)."""
    try:
        check_enabled(ctx.env, get_feature(ctx.env, name))
    except AIError as exc:
        return _error(exc)
    _record(ctx, data)
    url = database_url_for(ctx.request)
    uid, context = ctx.env.uid, dict(ctx.env.context)

    def events() -> Iterator[str]:
        from tessera.orm.environment import environment_for

        with environment_for(url, uid, context) as env:
            record = env[data.res_model].browse([data.res_id]) if data.res_model else None
            try:
                handler = HANDLERS.get(name)
                kwargs = handler(env, record, data.inputs) if handler else {}
                for ev in stream_feature(env, name, data.inputs, record=record, **kwargs):
                    yield f"event: {ev['event']}\ndata: {json.dumps(ev, default=str)}\n\n"
            except AIError as exc:
                yield f"event: error\ndata: {json.dumps({'event': 'error', 'error': exc.to_dict()})}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"cache-control": "no-cache"})


def _run_dict(run: Any, *, full: bool) -> dict[str, Any]:
    out = {
        "id": run.ids[0],
        "feature": run.feature,
        "user": run.user_id.display_name if run.user_id else None,
        "prompt_version": run.prompt_version,
        "provider": run.provider,
        "model": run.model,
        "status": run.status,
        "error_code": run.error_code,
        "decision": run.decision,
        "input_tokens": run.input_tokens,
        "output_tokens": run.output_tokens,
        "cost": run.cost,
        "latency_ms": run.latency_ms,
        "fallback_used": run.fallback_used,
        "redaction_applied": run.redaction_applied,
        "create_date": run.create_date.isoformat() if run.create_date else None,
        "res_model": run.res_model,
        "res_id": run.res_id or None,
    }
    if full:
        out.update(
            output_text=run.output_text,
            output_data=run.output_data,
            safety_flags=run.safety_flags,
            tool_calls=[
                {"step": c.step, "name": c.name, "allowed": c.allowed, "error": c.error, "arguments": c.arguments}
                for c in run.tool_call_ids
            ],
            citations=[
                {"id": c.source, "title": c.title, "res_model": c.res_model, "res_id": c.res_id}
                for c in run.citation_ids
            ],
            proposals=[proposal_dict(p) for p in run.proposal_ids],
        )
    return out


def _is_admin(ctx: RequestContext) -> bool:
    return bool(ctx.env.registry.security.has_groups(ctx.env, ["core.group_system"]))


def _visible_run(ctx: RequestContext, run_id: int) -> Any:
    run = ctx.env["ai_core.run"].elevated().browse([run_id]).exists()
    if not run:
        raise MissingError("No such run")
    if run.user_id.ids[:1] != [ctx.env.uid] and not _is_admin(ctx):
        if not run.res_model or run.res_model not in ctx.env.registry.models:
            raise AccessError("Not your run")
        ctx.env[run.res_model].browse([run.res_id]).check_access("read")  # audit trail of a record I can read
    return run


@router.get("/runs/{run_id}")
def get_run(run_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    return _run_dict(_visible_run(ctx, run_id), full=True)


@router.get("/runs")
def list_runs(
    res_model: str | None = None,
    res_id: int | None = None,
    limit: int = Query(20, le=100),
    ctx: RequestContext = Depends(authenticated),
) -> list[dict[str, Any]]:
    """The AI audit trail of a record (any user who can read the record), or my own runs."""
    runs = ctx.env["ai_core.run"].elevated()
    if res_model:
        if res_model not in ctx.env.registry.models or res_id is None:
            raise MissingError("Unknown record")
        ctx.env[res_model].browse([res_id]).check_access("read")
        found = runs.search([("res_model", "=", res_model), ("res_id", "=", res_id)], limit=limit)
    else:
        found = runs.search([("user_id", "=", ctx.env.uid)], limit=limit)
    return [_run_dict(r, full=False) for r in found]


@router.get("/proposals")
def list_proposals(
    state: str = "pending",
    res_model: str | None = None,
    res_id: int | None = None,
    ctx: RequestContext = Depends(authenticated),
) -> list[dict[str, Any]]:
    domain: list[Any] = [("user_id", "=", ctx.env.uid), ("state", "=", state)]
    if res_model:
        domain.append(("res_model", "=", res_model))
    if res_id:
        domain.append(("res_id", "=", res_id))
    return [proposal_dict(p) for p in ctx.env["ai_core.proposal"].elevated().search(domain, limit=100)]


class AcceptIn(BaseModel):
    payload: dict[str, Any] | None = None


@router.post("/proposals/{proposal_id}/accept", response_model=None)
def accept(
    proposal_id: int, data: AcceptIn | None = None, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any] | JSONResponse:
    p = P.accept(ctx.env, ctx.env["ai_core.proposal"].browse([proposal_id]), data.payload if data else None)
    out = proposal_dict(p)
    if p.state == "failed":
        return JSONResponse(status_code=409, content={"error": "ProposalFailed", "message": p.reason, "proposal": out})
    return out


@router.post("/proposals/{proposal_id}/reject")
def reject(
    proposal_id: int, reason: str = Body("", embed=True), ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    return proposal_dict(P.reject(ctx.env, ctx.env["ai_core.proposal"].browse([proposal_id]), reason))


class FeedbackIn(BaseModel):
    rating: str | None = Field(None, pattern="^(up|down)$")
    correction: str | None = Field(None, max_length=20_000)
    comment: str | None = Field(None, max_length=5_000)


@router.post("/runs/{run_id}/feedback", status_code=201)
def feedback(run_id: int, data: FeedbackIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    run = _visible_run(ctx, run_id)
    fb = (
        ctx.env["ai_core.feedback"]
        .elevated()
        .create(
            {"run_id": run.ids[0], "feature": run.feature, "user_id": ctx.env.uid, **data.model_dump(exclude_none=True)}
        )
    )
    return {"id": fb.ids[0]}


# ---------------------------------------------------------------------------- administration


@router.get("/admin/usage")
def usage(
    since: dt.date | None = None,
    until: dt.date | None = None,
    company_id: int | None = None,
    ctx: RequestContext = Depends(admin_only),
) -> dict[str, Any]:
    """Cost dashboard data (1.5-n): gateway ledger per company and feature, joined with acceptance
    rates from the run audit."""
    since = since or ctx.env.today().replace(day=1)
    params: dict[str, Any] = {
        "since": dt.datetime.combine(since, dt.time(), dt.UTC).isoformat(),
        "group_by": ["company_id", "feature"],
    }
    if until:
        params["until"] = dt.datetime.combine(until, dt.time(), dt.UTC).isoformat()
    if company_id:
        params["company_id"] = company_id
    try:
        rows = get_client(ctx.env).usage(params)
    except AIUnavailable as exc:
        return {"available": False, "message": exc.message, "rows": [], "since": since.isoformat()}
    runs = ctx.env["ai_core.run"].elevated()
    domain: list[Any] = [("create_date", ">=", dt.datetime.combine(since, dt.time(), dt.UTC))]
    if until:
        domain.append(("create_date", "<", dt.datetime.combine(until, dt.time(), dt.UTC)))
    decided: dict[tuple[int | None, str], dict[str, int]] = {}
    for r in runs.search([*domain, ("decision", "in", ["accepted", "edited", "rejected", "partial"])]):
        key = (r.company_id.ids[0] if r.company_id else None, r.feature)
        d = decided.setdefault(key, {"accepted": 0, "edited": 0, "rejected": 0, "partial": 0})
        d[r.decision] += 1
    companies = {c.ids[0]: c.name for c in ctx.env["core.company"].elevated().search([])}
    titles = {f.name: f.title for f in ctx.env["ai_core.feature"].elevated().search([])}
    for row in rows:
        d = decided.get((row.get("company_id"), row.get("feature", "")), {})
        total = sum(d.values())
        row["company"] = companies.get(row.get("company_id"), "")
        row["feature_title"] = titles.get(row.get("feature", ""), row.get("feature", ""))
        row["decisions"] = d
        row["acceptance_rate"] = round((d.get("accepted", 0) + d.get("edited", 0)) / total, 4) if total else None
    return {"available": True, "rows": rows, "since": since.isoformat(), "until": until.isoformat() if until else None}


@router.get("/admin/features")
def admin_features(ctx: RequestContext = Depends(admin_only)) -> list[dict[str, Any]]:
    companies = ctx.env["core.company"].elevated().search([])
    out = []
    for f in ctx.env["ai_core.feature"].elevated().search([]):
        out.append(
            {
                "name": f.name,
                "title": f.title,
                "module": f.module,
                "task_class": f.task_class,
                "risk_class": f.risk_class,
                "writes": f.writes,
                "outbound": f.outbound,
                "enabled_global": f.enabled_for(None),
                "companies": [{"id": c.ids[0], "name": c.name, "enabled": f.enabled_for(c.ids[0])} for c in companies],
            }
        )
    return out


class FlagIn(BaseModel):
    enabled: bool
    company_id: int | None = None


@router.put("/admin/features/{name}/flag")
def set_flag(name: str, data: FlagIn, ctx: RequestContext = Depends(admin_only)) -> dict[str, Any]:
    feature = get_feature(ctx.env, name)
    feature.set_enabled(data.company_id, data.enabled)
    return {"name": name, "company_id": data.company_id, "enabled": feature.enabled_for(data.company_id)}
