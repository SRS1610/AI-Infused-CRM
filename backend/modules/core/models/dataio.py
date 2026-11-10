"""Import jobs, saved import mappings and export templates."""

from __future__ import annotations

from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.jobs import enqueue, job
from tessera.orm import Model, api, fields


class ImportJob(Model):
    _name = "core.import_job"
    _description = "Import"
    _order = "id desc"
    _rec_name = "filename"

    model_name = fields.Char("Model", required=True)
    filename = fields.Char(required=True)
    attachment_id = fields.Many2one("core.attachment", ondelete="set_null")
    headers = fields.Json(default=list)
    preview = fields.Json(default=list, help="First rows of the file")
    row_count = fields.Integer()
    mapping = fields.Json(default=dict, help="header → field path")
    options = fields.Json(default=dict, help="encoding, delimiter, sheet, dayfirst, tz")
    state = fields.Selection(
        [
            ("draft", "To map"),
            ("tested", "Dry run done"),
            ("queued", "Queued"),
            ("running", "Running"),
            ("done", "Done"),
            ("failed", "Failed"),
        ],
        default="draft",
        required=True,
    )
    created_count = fields.Integer()
    updated_count = fields.Integer()
    error_count = fields.Integer()
    errors = fields.Json(default=list)
    job_id = fields.Many2one("core.job", ondelete="set_null", fk=False)
    user_id = fields.Many2one("core.user", default=lambda self: self.env.uid, ondelete="cascade")
    company_id = fields.Many2one(
        "core.company", ondelete="set_null", default=lambda self: self.env.company.ids[0] if self.env.company else None
    )

    def read_file(self) -> tuple[list[str], list[list[Any]]]:
        from tessera.dataio import read_table

        self.ensure_one()
        att = self.attachment_id.elevated()
        if not att:
            raise UserError(_("The import file is missing"))
        opts = self.options or {}
        return read_table(
            att.raw or b"",
            self.filename,
            encoding=opts.get("encoding"),
            delimiter=opts.get("delimiter"),
            sheet=opts.get("sheet"),
        )

    @api.expose
    def action_dry_run(self) -> dict[str, Any]:
        """Validate every row without writing anything."""
        from tessera.dataio import run_import

        self.ensure_one()
        headers, rows = self.read_file()
        result = run_import(
            self.env, self.model_name, headers, rows, self.mapping or {}, dry_run=True, options=self.options or {}
        )
        self.write({"state": "tested", "error_count": len(result.errors), "errors": result.errors[:1000]})
        return result.as_dict()

    @api.expose
    def action_start(self) -> dict[str, Any]:
        """Run the import in the background; progress is published on ``job:<id>``."""
        self.ensure_one()
        if self.state in ("queued", "running"):
            raise UserError(_("This import is already running"))
        job_rec = enqueue(
            self.env,
            "core.import_run",
            {"import_id": self.ids[0]},
            queue="imports",
            description=f"Import {self.filename}",
        )
        self.write({"state": "queued", "job_id": job_rec.ids[0]})
        return {"job_id": job_rec.ids[0]}


@job("core.import_run", queue="imports", max_attempts=1, timeout_seconds=3600)
def import_run_job(env: Any, import_id: int) -> dict[str, Any]:
    from tessera.dataio import run_import

    imp = env["core.import_job"].browse([import_id])
    imp.write({"state": "running"})
    headers, rows = imp.read_file()
    ledger = env["core.job"].elevated().browse(imp.elevated().job_id.ids)

    def progress(done: int, total: int) -> None:
        if ledger:
            ledger.set_progress(int(done * 100 / max(total, 1)), f"{done} / {total} rows")

    result = run_import(
        env, imp.model_name, headers, rows, imp.mapping or {}, options=imp.options or {}, progress=progress
    )
    imp.write(
        {
            "state": "done",
            "created_count": result.created,
            "updated_count": result.updated,
            "error_count": len(result.errors),
            "errors": result.errors[:1000],
        }
    )
    env["core.notification"].notify(
        [env.uid],
        _("Import finished: {filename}", filename=imp.filename),
        _(
            "{created} created, {updated} updated, {errors} errors",
            created=result.created,
            updated=result.updated,
            errors=len(result.errors),
        ),
        kind="job",
    )
    return {"created": result.created, "updated": result.updated, "errors": len(result.errors)}


class ImportMapping(Model):
    _name = "core.import_mapping"
    _description = "Saved import mapping"
    _order = "model_name, name"

    name = fields.Char(required=True)
    model_name = fields.Char("Model", required=True, index=True)
    headers_signature = fields.Char(index=True, help="Normalised header list the mapping was saved for")
    mapping = fields.Json(default=dict)
    user_id = fields.Many2one("core.user", default=lambda self: self.env.uid, ondelete="cascade")
    shared = fields.Boolean(help="Visible to every user")

    @staticmethod
    def signature(headers: list[str]) -> str:
        return "|".join(h.strip().lower() for h in headers)


class ExportTemplate(Model):
    _name = "core.export_template"
    _description = "Export template"
    _order = "model_name, name"

    name = fields.Char(required=True)
    model_name = fields.Char("Model", required=True, index=True)
    field_paths = fields.Json(default=list)
    user_id = fields.Many2one("core.user", default=lambda self: self.env.uid, ondelete="cascade")
    shared = fields.Boolean(help="Visible to every user")
