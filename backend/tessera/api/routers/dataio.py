"""Import (upload → map → dry run → background run) and export endpoints."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from tessera.api.deps import RequestContext, authenticated
from tessera.dataio import export_rows, importable_fields, read_table, render_export, suggest_mapping
from tessera.exceptions import MissingError, UserError
from tessera.i18n import _
from tessera.orm import fields as F

router = APIRouter(tags=["import-export"])

MAX_IMPORT = 50 * 1024 * 1024
PREVIEW_ROWS = 10


def _model(ctx: RequestContext, model: str) -> Any:
    if model not in ctx.registry.models:
        raise MissingError(_("Unknown model '{model}'", model=model))
    return ctx.env[model]


def _job_payload(job: Any) -> dict[str, Any]:
    return {
        "id": job.ids[0],
        "model": job.model_name,
        "filename": job.filename,
        "headers": job.headers or [],
        "preview": job.preview or [],
        "row_count": job.row_count,
        "mapping": job.mapping or {},
        "options": job.options or {},
        "state": job.state,
        "created": job.created_count,
        "updated": job.updated_count,
        "error_count": job.error_count,
        "errors": (job.errors or [])[:200],
        "job_id": job.job_id.ids[0] if job.job_id else None,
    }


@router.post("/import/{model}", status_code=201)
async def upload_import(
    model: str,
    file: UploadFile = File(...),
    options: str = Form("{}"),
    ctx: RequestContext = Depends(authenticated),
) -> dict[str, Any]:
    """Upload a CSV/XLSX file: returns headers, a preview, mapping suggestions and the importable fields."""
    target = _model(ctx, model)
    target.check_access_rights("create")
    data = await file.read()
    if len(data) > MAX_IMPORT:
        raise UserError(_("File too large (max 50 MB)"))
    try:
        opts = json.loads(options or "{}")
    except ValueError as exc:
        raise UserError(_("options must be JSON")) from exc
    filename = file.filename or "import.csv"
    headers, rows = await run_in_threadpool(
        read_table,
        data,
        filename,
        encoding=opts.get("encoding"),
        delimiter=opts.get("delimiter"),
        sheet=opts.get("sheet"),
    )
    signature = ctx.env["core.import_mapping"].signature(headers)
    saved = ctx.env["core.import_mapping"].search(
        [("model_name", "=", model), ("headers_signature", "=", signature)], limit=1
    )
    mapping = suggest_mapping(target, headers, saved.mapping if saved else None)
    job = ctx.env["core.import_job"].create(
        {
            "model_name": model,
            "filename": filename,
            "headers": headers,
            "preview": [[str(c) for c in r] for r in rows[:PREVIEW_ROWS]],
            "row_count": len(rows),
            "mapping": mapping,
            "options": opts,
        }
    )
    attachment = ctx.env["core.attachment"].create(
        {"name": filename, "mimetype": file.content_type, "model": "core.import_job", "res_id": job.ids[0], "raw": data}
    )
    job.write({"attachment_id": attachment.ids[0]})
    return {
        **_job_payload(job),
        "model_label": target._description,
        "fields": importable_fields(target),
        "saved_mapping": saved.name if saved else None,
    }


class ImportUpdate(BaseModel):
    mapping: dict[str, str | None] | None = None
    options: dict[str, Any] | None = None
    save_as: str | None = Field(
        None, max_length=120, description="Save the mapping under this name for files with the same headers"
    )
    shared: bool = False


def _job(ctx: RequestContext, job_id: int) -> Any:
    job = ctx.env["core.import_job"].browse([job_id]).exists()
    if not job:
        raise MissingError(_("Import not found"))
    job.check_access("read")
    return job


@router.get("/import/jobs/{job_id}")
def get_import(job_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    return _job_payload(_job(ctx, job_id))


@router.patch("/import/jobs/{job_id}")
def update_import(job_id: int, data: ImportUpdate, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    job = _job(ctx, job_id)
    vals: dict[str, Any] = {}
    if data.mapping is not None:
        vals["mapping"] = data.mapping
        vals["state"] = "draft"
    if data.options is not None:
        vals["options"] = data.options
    if vals:
        job.write(vals)
    if data.save_as:
        mappings = ctx.env["core.import_mapping"]
        signature = mappings.signature(job.headers or [])
        existing = mappings.search(
            [("model_name", "=", job.model_name), ("headers_signature", "=", signature), ("user_id", "=", ctx.uid)],
            limit=1,
        )
        values = {"name": data.save_as, "mapping": job.mapping, "shared": data.shared}
        if existing:
            existing.write(values)
        else:
            mappings.create({**values, "model_name": job.model_name, "headers_signature": signature})
    return _job_payload(job)


@router.post("/import/jobs/{job_id}/dry-run")
def dry_run(job_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    job = _job(ctx, job_id)
    result = job.action_dry_run()
    return {**_job_payload(job), "result": result}


@router.post("/import/jobs/{job_id}/start", status_code=202)
def start_import(job_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    job = _job(ctx, job_id)
    job.action_start()
    return _job_payload(job)


@router.get("/import/{model}/template")
def import_template(model: str, ctx: RequestContext = Depends(authenticated)) -> Response:
    """A CSV with one column per importable field (required first)."""
    target = _model(ctx, model)
    cols = sorted(importable_fields(target), key=lambda f: (not f["required"], f["label"] or f["name"]))
    content, mimetype, ext = render_export([c["label"] or c["name"] for c in cols], [], "csv")
    return Response(
        content, media_type=mimetype, headers={"Content-Disposition": f'attachment; filename="{model}-template.{ext}"'}
    )


# -- export ---------------------------------------------------------------------------------------


class ExportIn(BaseModel):
    model: str
    fields: list[str] = Field(min_length=1, max_length=200)
    domain: Any = None
    ids: list[int] | None = None
    format: str = Field("csv", pattern="^(csv|xlsx)$")
    order: str | None = None
    limit: int | None = Field(None, ge=1, le=100_000)


@router.post("/export")
def export(data: ExportIn, ctx: RequestContext = Depends(authenticated)) -> Response:
    target = _model(ctx, data.model)
    records = (
        target.browse(data.ids) if data.ids else target.search(data.domain or [], order=data.order, limit=data.limit)
    )
    kept, headers, rows = export_rows(records, data.fields)
    content, mimetype, ext = render_export(headers, rows, data.format)
    dropped = [p for p in data.fields if p not in kept]
    return Response(
        content,
        media_type=mimetype,
        headers={
            "Content-Disposition": f'attachment; filename="{data.model}.{ext}"',
            "X-Tessera-Dropped-Fields": ",".join(dropped),
        },
    )


@router.get("/export/{model}/fields")
def exportable_fields(
    model: str, prefix: str = "", ctx: RequestContext = Depends(authenticated)
) -> list[dict[str, Any]]:
    """Readable fields of ``model`` (or of the model reached through ``prefix``, e.g. ``author_id``)."""
    current = _model(ctx, model)
    for part in [p for p in prefix.split("/") if p]:
        f = current._fields.get(part)
        if (
            f is None
            or not isinstance(f, F.Many2one | F.Many2many | F.One2many)
            or not current._check_field_access(f, "read")
        ):
            raise UserError(_("Cannot follow '{part}'", part=part))
        current = ctx.env[str(f.comodel_name)]
    out = []
    for f in current._readable_fields():
        if isinstance(f, F.Binary) or f.name in ("deleted_at", "deleted_by"):
            continue
        path = f"{prefix}/{f.name}" if prefix else f.name
        out.append(
            {
                "path": path,
                "label": f.label,
                "type": f.type_name,
                "relational": isinstance(f, F.Many2one | F.Many2many | F.One2many),
            }
        )
    return sorted(out, key=lambda x: x["label"] or x["path"])
