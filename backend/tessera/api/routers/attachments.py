"""Attachment upload and download."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response

from tessera.api.deps import RequestContext, authenticated_any, request_context
from tessera.exceptions import AccessError, MissingError

router = APIRouter(prefix="/attachments", tags=["attachments"])

MAX_UPLOAD = 50 * 1024 * 1024


@router.post("", status_code=201)
async def upload(
    file: UploadFile = File(...),
    model: str | None = Form(None),
    res_id: int | None = Form(None),
    res_field: str | None = Form(None),
    public: bool = Form(False),
    ctx: RequestContext = Depends(authenticated_any),
) -> dict[str, Any]:
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise AccessError("File too large (max 50 MB)")
    if model and res_id:
        if model not in ctx.registry.models:
            raise MissingError(f"Unknown model '{model}'")
        ctx.env[model].browse([res_id]).check_access("write")
    attachment = ctx.env["core.attachment"].create(
        {
            "name": file.filename or "upload",
            "mimetype": file.content_type or None,
            "model": model,
            "res_id": res_id,
            "res_field": res_field,
            "public": public,
            "raw": data,
        }
    )
    return attachment.read(["name", "mimetype", "file_size", "checksum", "model", "res_id", "public"])[0]


@router.get("/{attachment_id}/content")
def download(attachment_id: int, token: str | None = None, ctx: RequestContext = Depends(request_context)) -> Response:
    sys_env = ctx.system_env()
    attachment = sys_env["core.attachment"].browse([attachment_id]).exists()
    if not attachment:
        raise MissingError("Attachment not found")
    if token:
        if attachment.access_token != token:
            raise AccessError("Invalid attachment token")
    elif attachment.public:
        pass
    else:
        if not ctx.authenticated:
            raise AccessError("Authentication required")
        user_att = ctx.env["core.attachment"].browse([attachment_id])
        user_att.check_record_access("read")
    data = attachment.raw or b""
    headers = {
        "Content-Disposition": f'inline; filename="{attachment.name}"',
        "Cache-Control": "private, max-age=300" if not attachment.public else "public, max-age=86400",
    }
    return Response(content=data, media_type=attachment.mimetype or "application/octet-stream", headers=headers)
