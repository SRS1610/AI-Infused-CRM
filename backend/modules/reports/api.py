"""Report API: list printable templates for a model, render (sync or in the worker), preview drafts."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import Response

from tessera.api.deps import RequestContext, admin_only, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError, UserError
from tessera.jobs import enqueue
from tessera.reports import blocks_to_template, render_html

router = register_router(APIRouter(prefix="/reports", tags=["reports"]))


def _template(ctx: RequestContext, ref: str) -> Any:
    templates = ctx.env["reports.template"]
    tpl = templates.browse([int(ref)]).exists() if ref.isdigit() else ctx.env.ref(ref, raise_if_not_found=False)
    if not tpl or tpl._name != "reports.template":
        raise MissingError(f"Report '{ref}' not found")
    return tpl


def _ids(raw: str) -> list[int]:
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError as exc:
        raise UserError("ids must be comma-separated integers") from exc
    if not ids or len(ids) > 500:
        raise UserError("Give between 1 and 500 ids")
    return ids


@router.get("/for/{model}")
def templates_for(model: str, ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    found = ctx.env["reports.template"].search([("model_name", "=", model), ("active", "=", True)])
    return [
        {"id": t.ids[0], "xmlid": t.get_external_id().get(t.ids[0]), "name": t.name}
        for t in found
        if t.current_version_id
    ]


@router.get("/{ref}/render")
def render(
    ref: str,
    ids: str = Query(...),
    format: str = Query("pdf", pattern="^(pdf|html)$"),
    download: bool = False,
    ctx: RequestContext = Depends(authenticated),
) -> Response:
    tpl = _template(ctx, ref)
    content, mimetype, filename = tpl.render(_ids(ids), format)
    disposition = "attachment" if download else "inline"
    return Response(
        content, media_type=mimetype, headers={"Content-Disposition": f'{disposition}; filename="{filename}"'}
    )


@router.post("/{ref}/render-async", status_code=202)
def render_async(
    ref: str,
    ids: list[int] = Body(..., embed=True),
    format: str = Body("pdf", embed=True),
    ctx: RequestContext = Depends(authenticated),
) -> dict[str, Any]:
    tpl = _template(ctx, ref)
    ctx.env[tpl.model_name].browse(ids).check_access("read")
    job = enqueue(
        ctx.env,
        "reports.render",
        {"template_id": tpl.ids[0], "res_ids": ids, "fmt": format},
        queue="reports",
        description=f"Print {tpl.name}",
    )
    return {"job_id": job.ids[0]}


@router.post("/preview")
def preview(
    model: str = Body(...),
    res_id: int = Body(...),
    body: str | None = Body(None),
    blocks: list[dict[str, Any]] | None = Body(None),
    layout_id: int | None = Body(None),
    ctx: RequestContext = Depends(admin_only),
) -> Response:
    """Render an unsaved template as HTML for the editor's live preview."""
    if model not in ctx.registry.models:
        raise MissingError(f"Unknown model '{model}'")
    record = ctx.env[model].browse([res_id])
    record.check_access("read")
    source = body or blocks_to_template(blocks or [])
    layout = ctx.env["reports.layout"].browse([layout_id]).exists() if layout_id else None
    from tessera.reports.engine import wrap

    html_doc = render_html(
        source,
        {"docs": [record], "company": wrap(ctx.env.company.elevated()), "user": wrap(ctx.env.user.elevated())},
        layout=layout.as_dict() if layout else None,
    )
    return Response(html_doc, media_type="text/html; charset=utf-8")
