"""HTTP API of the accounting AI features (``/api/v1/account/ai``).

* ``POST /bills/read`` (multipart): read an uploaded document → review + one proposal (nothing written);
* ``POST /bills/proposals/{id}/accept``: accept (or edit then accept) it → the draft document's id;
* ``GET /moves/{id}/anomalies``, ``POST /moves/{id}/anomalies/check``, ``POST /anomalies/{id}/{decision}``;
* ``GET /cashflow`` (engine numbers) and ``POST /cashflow/narrative`` (model narrative, numbers checked).

Every route answers 404 ``feature_disabled`` when the feature is off for the company (no gateway call).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from modules.account import ai_anomaly as A
from modules.account import ai_bill as B
from modules.account import ai_cashflow as C
from tessera.ai.errors import AIError
from tessera.ai.runtime import check_enabled, get_feature, run_feature
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError, ValidationError
from tessera.i18n import _

router = register_router(APIRouter(prefix="/account/ai", tags=["account", "ai"]))


def _error(exc: AIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.http_status, content={"error": type(exc).__name__, "message": exc.message, **exc.context}
    )


def _move(ctx: RequestContext, move_id: int) -> Any:
    move = ctx.env["account.move"].browse([move_id])
    if not move.exists():
        raise MissingError(_("No such document"))
    move.check_access("read")
    return move


def _flags(move: Any) -> list[dict[str, Any]]:
    out = []
    for f in move.ai_anomaly_ids.sorted(lambda f: (f.state != "open", -f.ids[0])):
        related = f.related_move_id
        out.append(
            {
                "id": f.ids[0],
                "kind": f.kind,
                "severity": f.severity,
                "score": f.score,
                "explanation": f.explanation,
                "state": f.state,
                "related": {"id": related.ids[0], "name": related.display_name} if related else None,
                "ai_generated": True,
            }
        )
    return out


# ----------------------------------------------------------------------------- documents


@router.post("/bills/read", response_model=None)
async def read_bill(
    file: UploadFile = File(...),
    move_type: str | None = Form(None),
    move_id: int | None = Form(None),
    ctx: RequestContext = Depends(authenticated),
) -> dict[str, Any] | JSONResponse:
    try:
        check_enabled(ctx.env, get_feature(ctx.env, B.FEATURE))
    except AIError as exc:
        return _error(exc)
    content = await file.read(B.MAX_BYTES + 1)
    if len(content) > B.MAX_BYTES:
        raise ValidationError(_("The document is too large (15 MB at most)"))
    target = _move(ctx, move_id) if move_id else None
    mt = B.media_type(content, file.content_type)
    if mt not in B.MEDIA_TYPES:
        raise ValidationError(_("Upload a PDF or an image (PNG, JPEG, WebP)"))
    ctx.env["account.move"].check_access("create")
    attachment = ctx.env["core.attachment"].create(
        {"name": file.filename or "document", "mimetype": mt, "raw": content}
    )
    try:
        return B.read_document(
            ctx.env,
            content,
            mt,
            file.filename or "document",
            move_type=move_type or None,
            target=target,
            attachment_id=attachment.ids[0],
        )
    except AIError as exc:
        return _error(exc)


class AcceptIn(BaseModel):
    payload: dict[str, Any] | None = None


@router.post("/bills/proposals/{proposal_id}/accept", response_model=None)
def accept_bill(
    proposal_id: int, data: AcceptIn | None = None, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any] | JSONResponse:
    out = B.accept(ctx.env, ctx.env["ai_core.proposal"].browse([proposal_id]), data.payload if data else None)
    if out["proposal"]["state"] == "failed":
        return JSONResponse(
            status_code=409, content={"error": "ProposalFailed", "message": out["proposal"]["reason"], **out}
        )
    return out


# ----------------------------------------------------------------------------- anomalies


@router.get("/moves/{move_id}/anomalies", response_model=None)
def anomalies(move_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    try:
        check_enabled(ctx.env, get_feature(ctx.env, A.FEATURE))
    except AIError as exc:
        return _error(exc)
    return {"flags": _flags(_move(ctx, move_id)), "ai_generated": True}


@router.post("/moves/{move_id}/anomalies/check", response_model=None)
def check(move_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    try:
        check_enabled(ctx.env, get_feature(ctx.env, A.FEATURE))
    except AIError as exc:
        return _error(exc)
    move = _move(ctx, move_id)
    new = A.check_move(ctx.env, move, notify=False)
    return {"flags": _flags(move), "new": len(new), "ai_generated": True}


class DecideIn(BaseModel):
    note: str | None = None


@router.post("/anomalies/{flag_id}/{decision}")
def decide(
    flag_id: int, decision: str, data: DecideIn | None = None, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    flag = ctx.env["account.move_anomaly"].browse([flag_id])
    if not flag.exists() or decision not in ("dismiss", "confirm"):
        raise MissingError(_("No such flag"))
    flag.check_access("read")
    note = data.note if data else None
    if decision == "dismiss":
        flag.action_dismiss(note)
    else:
        flag.action_confirm(note)
    return {"id": flag_id, "state": flag.state}


# ----------------------------------------------------------------------------- cash flow


@router.get("/cashflow", response_model=None)
def cashflow(weeks: int = 8, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    try:
        C.ensure_enabled(ctx.env)
    except AIError as exc:
        return _error(exc)
    ctx.env["account.move"].check_access("read")
    result = C.forecast(C.cash_data(ctx.env), weeks)
    return {**result, "summary": C.plain_summary(result), "ai_generated": True}


class NarrativeIn(BaseModel):
    weeks: int = Field(default=8, ge=1, le=C.MAX_WEEKS)


@router.post("/cashflow/narrative", response_model=None)
def narrative(
    data: NarrativeIn | None = None, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any] | JSONResponse:
    weeks = data.weeks if data else 8
    try:
        C.ensure_enabled(ctx.env)
        ctx.env["account.move"].check_access("read")
        result = C.forecast(C.cash_data(ctx.env), weeks)
        company = ctx.env.company or ctx.env.ref("core.main_company")
        run = run_feature(ctx.env, C.FEATURE, {"weeks": weeks}, **C.narrative_kwargs(result, company.ids[0]))
    except AIError as exc:
        return _error(exc)
    out = run.data if isinstance(run.data, dict) else {}
    return {
        "run_id": run.run.ids[0],
        "narrative": out.get("narrative") or C.plain_summary(result),
        "narrative_source": out.get("narrative_source", "facts"),
        "foreign_numbers": out.get("foreign_numbers", []),
        "forecast": result,
        "ai_generated": True,
    }
