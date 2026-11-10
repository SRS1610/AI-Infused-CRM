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
