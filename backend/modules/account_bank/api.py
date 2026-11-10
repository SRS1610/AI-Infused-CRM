"""Statement import endpoint: an uploaded attachment (``POST /api/v1/attachments``) and a bank journal."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError
from tessera.i18n import _

router = register_router(APIRouter(prefix="/account_bank", tags=["account_bank"]))


class ImportIn(BaseModel):
    journal_id: int
    attachment_id: int
    file_format: str | None = Field(None, pattern="^(camt053|ofx|mt940|csv)$")
    options: dict[str, Any] | None = None
    post: bool = True


@router.post("/import")
def import_statement(data: ImportIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Import a statement file; returns the statements created, duplicates skipped and lines reconciled."""
    env = ctx.env
    journal = env["account.journal"].browse([data.journal_id]).exists()
    attachment = env["core.attachment"].browse([data.attachment_id]).exists()
    if not journal or not attachment:
        raise MissingError(_("Journal or file not found"))
    journal.check_access("read")
    attachment.check_access("read")
    record = env["account.bank_statement_import"].create(
        {
            "journal_id": journal.ids[0],
            "attachment_id": attachment.ids[0],
            "filename": attachment.name,
            "file_format": data.file_format,
            "options": data.options,
            "post": data.post,
        }
    )
    result = record.action_import()
    return {"import_id": record.ids[0], **result}
