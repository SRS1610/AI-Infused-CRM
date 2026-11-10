"""Contacts API: duplicate candidates and the merge wizard."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends
from pydantic import BaseModel, Field

from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import UserError

router = register_router(APIRouter(prefix="/contacts", tags=["contacts"]))
COMPARED = [
    "name",
    "email",
    "phone",
    "mobile",
    "street",
    "city",
    "zip",
    "country_id",
    "vat",
    "website",
    "function",
    "parent_id",
    "user_id",
]


class MergeIn(BaseModel):
    ids: list[int] = Field(min_length=2, max_length=11)
    survivor_id: int
    field_choices: dict[str, int] = {}


@router.get("/{partner_id}/duplicates")
def duplicates(partner_id: int, ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    return ctx.env["contacts.partner"].browse([partner_id]).find_duplicates()


@router.post("/merge/preview")
def merge_preview(ids: list[int] = Body(...), ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Side-by-side values of the contacts to merge, for the wizard."""
    if len(set(ids)) < 2:
        raise UserError("Select at least two contacts")
    partners = ctx.env["contacts.partner"].browse(ids)
    partners.check_access("write")
    fields = [f for f in COMPARED if f in partners._fields]
    rows = partners.read(["display_name", *fields])
    conflicts = [f for f in fields if len({str(r.get(f)) for r in rows if r.get(f) not in (None, False, "")}) > 1]
    return {"records": rows, "fields": fields, "conflicts": conflicts}


@router.post("/merge")
def merge(data: MergeIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    if data.survivor_id not in data.ids:
        raise UserError("The contact to keep must be one of the selected contacts")
    partners = ctx.env["contacts.partner"].browse(data.ids)
    return partners.action_merge(data.survivor_id, data.field_choices)


@router.post("/merge/{merge_id}/undo")
def undo(merge_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    return ctx.env["contacts.merge_log"].browse([merge_id]).action_undo()


# -- AI: duplicates, merge proposals, background enrichment (Phase 1.6) ----------------------------


def _ai_partner(ctx: RequestContext, partner_id: int, feature: str) -> Any:
    from tessera.ai.runtime import check_enabled, get_feature
    from tessera.exceptions import MissingError

    check_enabled(ctx.env, get_feature(ctx.env, feature))  # 404 feature_disabled when switched off
    partner = ctx.env["contacts.partner"].browse([partner_id])
    if not partner.exists():
        raise MissingError("No such contact")
    partner.check_access("read")
    return partner


@router.get("/{partner_id}/ai/duplicates")
def ai_duplicates(partner_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Ranked duplicate candidates (fuzzy + semantic) with reasons, among contacts the user can read."""
    from modules.contacts.duplicates import FEATURE, detect

    return detect(_ai_partner(ctx, partner_id, FEATURE))


class ProposeMergeIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=10)


@router.post("/{partner_id}/ai/duplicates/propose-merge", status_code=201)
def ai_propose_merge(
    partner_id: int, data: ProposeMergeIn, ctx: RequestContext = Depends(authenticated)
) -> dict[str, Any]:
    """A merge proposal (kind ``action``): nothing changes until the user accepts it."""
    from modules.contacts.duplicates import FEATURE, propose_merge
    from tessera.ai.runtime import proposal_dict

    partner = _ai_partner(ctx, partner_id, FEATURE)
    return proposal_dict(propose_merge(ctx.env, partner, data.ids))


@router.post("/{partner_id}/ai/enrich", status_code=202)
def ai_enrich(partner_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Queue a background enrichment; the user is notified with a link when suggestions are ready."""
    from modules.contacts.ai_features import ENRICH_FEATURE
    from tessera.jobs import enqueue

    _ai_partner(ctx, partner_id, ENRICH_FEATURE)
    queued = enqueue(ctx.env, "contacts.enrich_partner", {"partner_id": partner_id}, description="Contact enrichment")
    return {"job_id": queued.ids[0], "state": "queued"}


@router.get("/ai/enrichment-adapters")
def ai_enrichment_adapters(ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    """Registered adapters, what they send, and whether this company may use them."""
    from modules.contacts.enrichment import ADAPTERS, approved_sources

    approved = {s.adapter for s in approved_sources(ctx.env)}
    return [
        {"key": a.key, "label": a.label, "sends": list(a.sends), "approved": a.key in approved}
        for a in ADAPTERS.values()
    ]
