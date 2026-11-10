"""Client metadata API: menus, actions and resolved views."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from modules.web.views import resolve_view, strip_inaccessible_fields
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError, ValidationError

router = register_router(APIRouter(prefix="/web", tags=["web"]))


def _group_xmlids(ctx: RequestContext, groups: Any) -> list[str]:
    return [x for x in groups.get_external_id().values() if x] if groups else []


def _visible(ctx: RequestContext, record: Any) -> bool:
    groups = record.group_ids
    if not groups:
        return True
    return bool(ctx.registry.security.has_groups(ctx.env, _group_xmlids(ctx, groups)))


def _action_payload(ctx: RequestContext, action: Any) -> dict[str, Any]:
    xmlid = action.get_external_id().get(action.ids[0])
    return {
        "id": action.ids[0],
        "xmlid": xmlid,
        "name": action.name,
        "kind": action.kind,
        "model": action.model_name,
        "view_types": action.view_types or [],
        "view_refs": action.view_refs or {},
        "search_view_ref": action.search_view_ref,
        "domain": action.domain,
        "context": action.context or {},
        "target": action.target,
        "url": action.url,
        "client_tag": action.client_tag,
        "server_method": action.server_method,
        "report_ref": action.report_ref,
        "limit": action.limit,
        "help": action.help,
    }


@router.get("/menus")
def menus(ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    """Menu tree visible to the caller (group filtered; a menu without visible children and
    without an action is dropped)."""
    menu_model = ctx.env["web.menu"].elevated()
    all_menus = menu_model.search([], order="sequence, id")
    xmlids = all_menus.get_external_id()
    visible_ids = {m.ids[0] for m in all_menus if _visible(ctx, m) and (not m.action_id or _visible(ctx, m.action_id))}

    def build(parent_id: int | None) -> list[dict[str, Any]]:
        out = []
        for m in all_menus:
            if (m.parent_id.ids[0] if m.parent_id else None) != parent_id or m.ids[0] not in visible_ids:
                continue
            children = build(m.ids[0])
            if not children and not m.action_id:
                continue
            action = m.action_id
            out.append(
                {
                    "id": m.ids[0],
                    "xmlid": xmlids.get(m.ids[0]),
                    "name": m.name,
                    "sequence": m.sequence,
                    "icon": m.icon,
                    "color": m.color,
                    "action": action.get_external_id().get(action.ids[0]) if action else None,
                    "action_id": action.ids[0] if action else None,
                    "children": children,
                }
            )
        return out

    return build(None)


@router.get("/actions/{ref}")
def action(ref: str, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    actions = ctx.env["web.action"].elevated()
    if ref.isdigit():
        record = actions.browse([int(ref)]).exists()
    else:
        found = ctx.env.ref(ref, raise_if_not_found=False)
        record = actions.browse(found.ids) if found is not None and found._name == "web.action" else actions.browse([])
    if not record or not _visible(ctx, record):
        raise MissingError(f"Action '{ref}' not found")
    if record.model_name and record.kind in ("window", "server", "report"):
        ctx.env[record.model_name].check_access_rights("read")
    return _action_payload(ctx, record)


@router.get("/views")
def views(
    model: str,
    types: str = Query("form,list,search", description="Comma-separated view types"),
    refs: str | None = Query(None, description="type:xmlid pairs, comma-separated"),
    ctx: RequestContext = Depends(authenticated),
) -> dict[str, Any]:
    if model not in ctx.registry.models:
        raise MissingError(f"Unknown model '{model}'")
    record_model = ctx.env[model]
    record_model.check_access_rights("read")
    ref_map: dict[str, str] = {}
    for pair in (refs or "").split(","):
        if ":" in pair:
            t, x = pair.split(":", 1)
            ref_map[t.strip()] = x.strip()
    wanted = [t.strip() for t in types.split(",") if t.strip()]
    out: dict[str, Any] = {}
    for view_type in wanted:
        try:
            definition = resolve_view(ctx.env, model, view_type, ref_map.get(view_type))
        except ValidationError as exc:
            raise MissingError(exc.message) from exc
        out[view_type] = strip_inaccessible_fields(ctx.env, model, definition)
    return {
        "model": {
            "name": model,
            "description": type(record_model)._description,
            "rec_name": type(record_model)._rec_name,
            "order": type(record_model)._order,
            "methods": sorted(type(record_model)._exposed_methods),
            "access": {
                op: record_model.check_access_rights(op, raise_exception=False)
                for op in ("read", "write", "create", "unlink")
            },
        },
        "fields": record_model.fields_get(),
        "views": out,
    }


@router.get("/filters")
def filters(model: str, ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    return ctx.env["web.filter"].search_read(
        [("model_name", "=", model)],
        ["name", "domain", "context", "group_bys", "sort", "is_default", "is_favorite", "user_id", "action_id"],
    )
