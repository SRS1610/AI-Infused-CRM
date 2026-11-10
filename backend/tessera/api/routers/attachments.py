"""Attachment upload and download."""

from __future__ import annotations

import hmac
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import Response

from tessera.api.deps import RequestContext, authenticated_any, request_context
from tessera.api.serializers import content_disposition
from tessera.exceptions import AccessError, MissingError
from tessera.i18n import _

router = APIRouter(prefix="/attachments", tags=["attachments"])

MAX_UPLOAD = 50 * 1024 * 1024

# Types a browser may show in place. Anything else (HTML, SVG, XML, scripts …) is served as a download so
# that an uploaded file can never run script in the application's origin.
INLINE_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/bmp",
        "image/avif",
        "text/plain",
        "text/csv",
    }
)
INLINE_PREFIXES = ("audio/", "video/")
# Defence in depth for whatever is rendered: no script, no plugins, no framing by other sites.
DOWNLOAD_CSP = (
    "default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'self'"
)


def _inline_allowed(mimetype: str) -> bool:
    return mimetype in INLINE_TYPES or mimetype.startswith(INLINE_PREFIXES)


@router.post("", status_code=201)
async def upload(
    file: UploadFile = File(...),
    model: str | None = Form(None),
    res_id: int | None = Form(None),
    res_field: str | None = Form(None),
    public: bool = Form(False),
    ctx: RequestContext = Depends(authenticated_any),
) -> dict[str, Any]:
    if file.size is not None and file.size > MAX_UPLOAD:
        raise AccessError(_("File too large (max 50 MB)"))
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise AccessError(_("File too large (max 50 MB)"))
    if model and res_id:
        if model not in ctx.registry.models:
            raise MissingError(_("Unknown model '{model}'", model=model))
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
        raise MissingError(_("Attachment not found"))
    if token:
        expected = attachment.access_token or ""
        if not expected or not hmac.compare_digest(expected, token):
            raise AccessError(_("Invalid attachment token"))
    elif attachment.public:
        pass
    else:
        if not ctx.authenticated:
            raise AccessError(_("Authentication required"))
        ctx.env["core.attachment"].browse([attachment_id]).check_access("read")
    data = attachment.raw or b""
    mimetype = (attachment.mimetype or "application/octet-stream").split(";")[0].strip().lower()
    inline = _inline_allowed(mimetype)
    headers = {
        "Content-Disposition": content_disposition(attachment.name or "download", inline=inline),
        "Cache-Control": "private, max-age=300" if not attachment.public else "public, max-age=86400",
        "X-Content-Type-Options": "nosniff",
    }
    if mimetype != "application/pdf":  # browsers' built-in PDF viewers do not run under a restrictive policy
        headers["Content-Security-Policy"] = DOWNLOAD_CSP
    return Response(content=data, media_type=mimetype, headers=headers)
