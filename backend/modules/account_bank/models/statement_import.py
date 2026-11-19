"""Statement imports: one record per uploaded file (journal, file, detected format, result)."""

from __future__ import annotations

from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class BankStatementImport(Model):
    _name = "account.bank_statement_import"
    _description = "Bank statement import"
    _order = "id desc"

    journal_id = fields.Many2one(
        "account.journal", required=True, ondelete="cascade", domain=[["type", "in", ["bank", "cash"]]], index=True
    )
    company_id = fields.Many2one("core.company", related="journal_id.company_id", store=True, index=True)
    data_file = fields.Binary("File", help="CSV, OFX, CAMT.053 (XML) or MT940")
    filename = fields.Char("File name")
    attachment_id = fields.Many2one("core.attachment", "Stored file", ondelete="set_null", readonly=True)
    file_format = fields.Selection(
        [("camt053", "CAMT.053"), ("ofx", "OFX"), ("mt940", "MT940"), ("csv", "CSV")],
        "Format",
        help="Detected from the content when empty",
    )
    options = fields.Json("CSV options", help="Column mapping and formats for CSV files (see the documentation)")
    post = fields.Boolean("Post and auto-reconcile", default=True)
    state = fields.Selection(
        [("draft", "To import"), ("done", "Imported"), ("error", "Failed")],
        default="draft",
        required=True,
        readonly=True,
    )
    statement_ids = fields.Many2many(
        "account.bank_statement",
        "Statements",
        relation="account_bank_statement_import_rel",
        column1="import_id",
        column2="statement_id",
        readonly=True,
    )
    line_count = fields.Integer("Transactions imported", readonly=True)
    duplicate_count = fields.Integer("Already imported (skipped)", readonly=True)
    reconciled_count = fields.Integer("Reconciled automatically", readonly=True)
    message = fields.Text(readonly=True)

    @api.depends("filename", "journal_id")
    def _compute_display_name(self) -> None:
        for rec in self:
            rec.display_name = rec.filename or _("Import {id}", id=rec.ids[0] if rec.ids else "")

    def _content(self) -> bytes:
        if self.attachment_id:
            return bytes(self.attachment_id.elevated().raw or b"")
        if self.data_file:
            return bytes(self.data_file)
        raise UserError(_("Choose a file to import"))

    @api.expose
    def action_import(self) -> dict[str, Any]:
        from modules.account_bank.importing import import_statements

        self.ensure_one()
        if self.state == "done":
            raise UserError(_("This file has already been imported"))
        content = self._content()
        attachment = self.attachment_id
        if not attachment:
            attachment = (
                self.env["core.attachment"]
                .elevated()
                .create(
                    {
                        "name": self.filename or "statement",
                        "model": self._name,
                        "res_id": self.ids[0],
                        "raw": content,
                        "company_id": self.company_id.ids[0] if self.company_id else None,
                    }
                )
            )
        result = import_statements(
            self.journal_id,
            content,
            self.filename or attachment.name,
            file_format=self.file_format or None,
            options=dict(self.options) if self.options else None,
            attachment=attachment,
            post=bool(self.post),
            auto_reconcile=bool(self.post),
        )
        self.write(
            {
                "state": "done",
                "attachment_id": attachment.ids[0],
                "data_file": None,
                "file_format": result["format"],
                "statement_ids": [("set", result["statement_ids"])],
                "line_count": result["lines"],
                "duplicate_count": result["duplicates"],
                "reconciled_count": result["reconciled"],
                "message": _(
                    "{lines} transactions imported, {duplicates} already imported, "
                    "{reconciled} reconciled automatically",
                    lines=result["lines"],
                    duplicates=result["duplicates"],
                    reconciled=result["reconciled"],
                ),
            }
        )
        return result
