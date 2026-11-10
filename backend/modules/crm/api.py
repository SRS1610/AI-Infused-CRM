"""CRM API: embeddable web form (public), intake for integrations, pipeline reports."""

from __future__ import annotations

import datetime as dt
import hmac
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from modules.crm import reports
from tessera.api.deps import RequestContext, authenticated, request_context
from tessera.api.middleware import limiter
from tessera.api.registry import register_router
from tessera.config import get_settings
from tessera.exceptions import MissingError, UserError
from tessera.runtime import current_remote_ip

router = register_router(APIRouter(prefix="/crm", tags=["crm"]))
EMBED_JS = (Path(__file__).parent / "static" / "embed.js").read_text()
CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}


class WebformIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=200)
    phone: str | None = Field(None, max_length=60)
    company: str | None = Field(None, max_length=200)
    message: str | None = Field(None, max_length=10_000)
    subject: str | None = Field(None, max_length=200)
    page: str | None = Field(None, max_length=500)
    website_url: str | None = Field(None, description="Honeypot: real visitors leave it empty")


def _team_for_token(ctx: RequestContext, token: str) -> Any:
    teams = ctx.env["crm.team"].elevated("webform").search([("active", "=", True)])
    team = next((t for t in teams if t.webform_token and hmac.compare_digest(t.webform_token, token)), None)
    if team is None:
        raise MissingError("Unknown form")
    return team


@router.options("/webform/{token}")
def webform_preflight(token: str) -> Response:
    return Response(status_code=204, headers=CORS)


@router.post("/webform/{token}")
def webform(token: str, data: WebformIn, ctx: RequestContext = Depends(request_context)) -> JSONResponse:
    """1.4-f: public form endpoint. Creates a lead (and fires the on_create automations)."""
    if not limiter.allow(f"webform:{current_remote_ip() or '?'}", 20):
        return JSONResponse({"error": "RateLimited", "message": "Too many submissions"}, status_code=429, headers=CORS)
    team = _team_for_token(ctx, token)
    if data.website_url:  # bots fill every field; pretend success
        return JSONResponse({"ok": True}, headers=CORS)
    env = ctx.env.elevated("webform intake")
    from modules.contacts.models.partner import normalize_email

    email = normalize_email(data.email)
    if not email or "@" not in email:
        return JSONResponse(
            {"error": "ValidationError", "message": "Enter a valid email"}, status_code=422, headers=CORS
        )
    import html as _html

    lead = env["crm.lead"].create(
        {
            "name": data.subject or f"{data.company or data.name} — web enquiry",
            "type": "lead",
            "contact_name": data.name,
            "partner_name": data.company,
            "email_from": email,
            "phone": data.phone,
            "description": f"<p>{_html.escape(data.message or '')}</p>",
            "team_id": team.ids[0],
        }
    )
    env["crm.lead_intake"].create(
        {"lead_id": lead.ids[0], "channel": "webform", "source_ref": data.page, "payload": {"ip": current_remote_ip()}}
    )
    return JSONResponse({"ok": True, "lead_id": lead.ids[0]}, status_code=201, headers=CORS)


@router.get("/webform/{token}/embed.js")
def webform_embed(token: str, ctx: RequestContext = Depends(request_context)) -> Response:
    """A self-contained script: ``<div data-tessera-form></div><script src=".../embed.js"></script>``."""
    _team_for_token(ctx, token)
    endpoint = f"{get_settings().public_url.rstrip('/')}/api/v1/crm/webform/{token}"
    script = EMBED_JS.replace("__ENDPOINT__", json.dumps(endpoint))
    return Response(script, media_type="application/javascript", headers={"Cache-Control": "public, max-age=300"})


class IntakeIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    email: str | None = None
    contact_name: str | None = None
    company: str | None = None
    phone: str | None = None
    description: str | None = None
    team_id: int | None = None
    expected_revenue: float | None = None
    source: str | None = Field(None, max_length=120)


@router.post("/leads/intake", status_code=201)
def intake(data: IntakeIn, request: Request, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Create a lead from an integration (API key) with an intake log entry."""
    lead = ctx.env["crm.lead"].create(
        {
            "name": data.name,
            "type": "lead",
            "email_from": data.email,
            "contact_name": data.contact_name,
            "partner_name": data.company,
            "phone": data.phone,
            "description": data.description,
            "team_id": data.team_id,
            "expected_revenue": data.expected_revenue or 0.0,
        }
    )
    ctx.env["crm.lead_intake"].elevated().create(
        {
            "lead_id": lead.ids[0],
            "channel": "api",
            "source_ref": data.source or request.headers.get("user-agent", "")[:200],
        }
    )
    return {
        "id": lead.ids[0],
        "user_id": lead.user_id.ids[0] if lead.user_id else None,
        "team_id": lead.team_id.ids[0] if lead.team_id else None,
    }


def _date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError as exc:
        raise UserError("Dates use YYYY-MM-DD") from exc


@router.get("/reports/win-rate")
def report_win_rate(
    group_by: str = Query("user_id", pattern="^(user_id|team_id)$"),
    date_from: str | None = None,
    date_to: str | None = None,
    ctx: RequestContext = Depends(authenticated),
) -> list[dict[str, Any]]:
    return reports.win_rate(ctx.env, group_by, _date(date_from), _date(date_to))


@router.get("/reports/velocity")
def report_velocity(
    date_from: str | None = None, date_to: str | None = None, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    return reports.sales_velocity(ctx.env, _date(date_from), _date(date_to))


@router.get("/reports/funnel")
def report_funnel(team_id: int | None = None, ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    return reports.funnel(ctx.env, team_id)


@router.get("/reports/forecast")
def report_forecast(team_id: int | None = None, ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    return reports.forecast(ctx.env, team_id)
