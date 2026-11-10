"""Portal API for signed-in customers (portal users) and the public invitation acceptance."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from modules.portal.models.access import accept_invitation
from tessera.api.deps import RequestContext, authenticated_any, request_context
from tessera.api.middleware import limiter
from tessera.api.registry import register_router
from tessera.exceptions import UserError
from tessera.runtime import current_remote_ip

router = register_router(APIRouter(prefix="/portal", tags=["portal"]))
PROFILE_FIELDS = ["name", "email", "phone", "mobile", "function", "street", "street2", "city", "zip", "country_id"]
EDITABLE = {"phone", "mobile", "function", "street", "street2", "city", "zip", "country_id"}


@router.post("/invite/accept")
def accept(token: str = Body(..., embed=True), password: str = Body(..., embed=True), ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    if not limiter.allow(f"portal-invite:{current_remote_ip() or '?'}", 10):
        raise UserError("Too many attempts; try again in a minute")
    user = accept_invitation(ctx.env, token, password)
    return {"login": user.login}


def _partner(ctx: RequestContext) -> Any:
    partner = ctx.env.user.elevated().partner_id
    if not partner:
        raise UserError("Your account has no contact")
    return partner


@router.get("/me")
def profile(ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    partner = _partner(ctx)
    data = partner.read(PROFILE_FIELDS)[0]
    company = partner.commercial_partner_id
    data["company"] = company.name if company and company.ids != partner.ids else None
    data["editable"] = sorted(EDITABLE)
    return data


class ProfileIn(BaseModel):
    phone: str | None = Field(None, max_length=60)
    mobile: str | None = Field(None, max_length=60)
    function: str | None = Field(None, max_length=120)
    street: str | None = Field(None, max_length=200)
    street2: str | None = Field(None, max_length=200)
    city: str | None = Field(None, max_length=120)
    zip: str | None = Field(None, max_length=20)
    country_id: int | None = None


@router.patch("/me")
def update_profile(data: ProfileIn, ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    """Customers update their own contact details (only these fields, only their own contact)."""
    values = data.model_dump(exclude_unset=True)
    if values:
        _partner(ctx).write(values)  # elevated: portal users have no write access on contacts
    return profile(ctx)


@router.get("/documents")
def documents(ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    """Everything the signed-in customer may see: quotations of their company (via record rules)."""
    quotes = ctx.env["sale.quotation"].search([], order="date_order desc", limit=200)
    return {
        "quotations": [
            {
                "id": q.ids[0],
                "name": q.name,
                "date_order": q.date_order.isoformat() if q.date_order else None,
                "validity_date": q.validity_date.isoformat() if q.validity_date else None,
                "amount_total": q.amount_total,
                "currency": q.currency_id.name,
                "state": q.state,
            }
            for q in quotes
        ]
    }
