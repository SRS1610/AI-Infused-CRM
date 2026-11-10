"""Discuss AI endpoints that need more than the generic ``/ai/features/<name>/run``: the summary decides
between an interactive run and a background job, and both summary and catch-up return the channel
of every cited message so the client can link to it. Reply drafts and translations use the generic
``/ai/features/<name>/stream`` and ``/run`` endpoints (their handlers live in ``ai_features``)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from modules.discuss import ai_features as AF
from tessera.ai.errors import AIError
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router

router = register_router(APIRouter(prefix="/discuss/ai", tags=["discuss", "ai"]))


def _error(exc: AIError) -> JSONResponse:
    # returned, not raised: the transaction commits and the failed run stays in the audit trail
    return JSONResponse(
        status_code=exc.http_status, content={"error": type(exc).__name__, "message": exc.message, **exc.context}
    )


class SummaryIn(BaseModel):
    thread_id: int | None = None
    since: str | None = Field(None, max_length=40)
    until: str | None = Field(None, max_length=40)
    limit: int | None = Field(None, ge=1, le=AF.SUMMARY_MAX)


@router.post("/channels/{channel_id}/summary", response_model=None)
def channel_summary(
    channel_id: int, data: SummaryIn, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any] | JSONResponse:
    """``mode``: ``interactive`` (with ``result`` and ``links``), ``background`` (a job notifies the user
    with a link when the summary is ready) or ``empty`` (no message in range)."""
    inputs = data.model_dump(exclude_none=True)
    try:
        return AF.summarize(ctx.env, channel_id, inputs)
    except AIError as exc:
        return _error(exc)


class CatchUpIn(BaseModel):
    max_messages: int | None = Field(None, ge=1, le=AF.CATCH_UP_MAX)


@router.post("/catch-up", response_model=None)
def catch_up(data: CatchUpIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    try:
        return AF.catch_up(ctx.env, data.model_dump(exclude_none=True))
    except AIError as exc:
        return _error(exc)
