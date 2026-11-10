"""Studio API (designers only: every endpoint answers 403 to anyone else, 1.7-e)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field

from modules.studio import service as S
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError

router = register_router(APIRouter(prefix="/studio", tags=["studio"]))


def designer(ctx: RequestContext = Depends(authenticated)) -> RequestContext:
    S.check_designer(ctx.env)
    return ctx


def _change(c: Any) -> dict[str, Any]:
    return {
        "id": c.ids[0],
        "kind": c.kind,
        "model": c.model_name,
        "summary": c.summary,
        "state": c.state,
        "user": c.create_uid.display_name if c.create_uid else None,
        "date": c.create_date.isoformat() if c.create_date else None,
        "res_model": c.res_model,
        "res_id": c.res_id or None,
    }


@router.get("/models")
def models(ctx: RequestContext = Depends(designer)) -> list[dict[str, Any]]:
    return S.editable_models(ctx.env)


@router.get("/models/{model}/fields")
def model_fields(model: str, ctx: RequestContext = Depends(designer)) -> list[dict[str, Any]]:
    cls = S._model_cls(ctx.env, model)
    studio = set(
        ctx.env["studio.field"].elevated().search([("model_name", "=", model), ("active", "=", True)]).mapped("name")
    )
    out = []
    for name, f in sorted(cls._fields.items()):
        if name == "id" or f.type_name in ("vector",):
            continue
        out.append({"name": name, "label": f.label, "type": f.type_name, "studio": name in studio, "store": f.store})
    return out


class FieldIn(BaseModel):
    model: str
    name: str = Field(min_length=1, max_length=60)
    type: str
    label: str = Field(min_length=1, max_length=120)
    placements: list[dict[str, Any]] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)


@router.post("/fields", status_code=201)
def add_field(data: FieldIn, ctx: RequestContext = Depends(designer)) -> dict[str, Any]:
    return S.add_field(
        ctx.env, data.model, data.name, data.type, data.label, placements=data.placements, **data.options
    )


class OpsIn(BaseModel):
    operations: list[dict[str, Any]]
    summary: str = "Layout changed in Studio"


@router.post("/views/{model}/{view_type}/operations", status_code=201)
def change_view(model: str, view_type: str, data: OpsIn, ctx: RequestContext = Depends(designer)) -> dict[str, Any]:
    return _change(S.change_view(ctx.env, model, view_type, data.operations, data.summary))


class ViewIn(BaseModel):
    model: str
    view_type: str
    name: str
    definition: dict[str, Any]


@router.post("/views", status_code=201)
def create_view(data: ViewIn, ctx: RequestContext = Depends(designer)) -> dict[str, Any]:
    return _change(S.create_view(ctx.env, data.model, data.view_type, data.definition, data.name))


@router.post("/automations", status_code=201)
def add_automation(values: dict[str, Any], ctx: RequestContext = Depends(designer)) -> dict[str, Any]:
    return _change(S.add_automation(ctx.env, values))


class WorkflowIn(BaseModel):
    model: str
    name: str
    state_field: str
    states: list[dict[str, Any]]
    transitions: list[dict[str, Any]]


@router.post("/workflows", status_code=201)
def create_workflow(data: WorkflowIn, ctx: RequestContext = Depends(designer)) -> dict[str, Any]:
    return _change(S.create_workflow(ctx.env, data.model, data.state_field, data.states, data.transitions, data.name))


@router.get("/changes")
def changes(model: str | None = None, ctx: RequestContext = Depends(designer)) -> list[dict[str, Any]]:
    domain = [("model_name", "=", model)] if model else []
    return [_change(c) for c in ctx.env["studio.change"].elevated().search(domain, limit=200)]


@router.post("/changes/{change_id}/rollback")
def rollback(change_id: int, ctx: RequestContext = Depends(designer)) -> dict[str, Any]:
    change = ctx.env["studio.change"].elevated().browse([change_id]).exists()
    if not change:
        raise MissingError("No such change")
    done = S.rollback(ctx.env, change)
    return {**_change(change), "rolled_back_now": done}


@router.post("/snapshots", status_code=201)
def snapshot(name: str = "Snapshot", ctx: RequestContext = Depends(designer)) -> dict[str, Any]:
    snap = S.snapshot(ctx.env, name)
    return {"id": snap.ids[0], "name": snap.name, "last_change_id": snap.last_change_id}


@router.get("/snapshots")
def snapshots(ctx: RequestContext = Depends(designer)) -> list[dict[str, Any]]:
    return [
        {"id": s.ids[0], "name": s.name, "last_change_id": s.last_change_id, "date": s.create_date.isoformat()}
        for s in ctx.env["studio.snapshot"].elevated().search([], limit=50)
    ]


@router.post("/snapshots/{snapshot_id}/rollback")
def rollback_snapshot(snapshot_id: int, ctx: RequestContext = Depends(designer)) -> dict[str, Any]:
    snap = ctx.env["studio.snapshot"].elevated().browse([snapshot_id]).exists()
    if not snap:
        raise MissingError("No such snapshot")
    return {"rolled_back": S.rollback_to(ctx.env, snap)}


@router.get("/export")
def export(company: str = "default", ctx: RequestContext = Depends(designer)) -> Response:
    name, payload = S.export_module(ctx.env, company)
    return Response(
        payload, media_type="application/zip", headers={"content-disposition": f'attachment; filename="{name}.zip"'}
    )
