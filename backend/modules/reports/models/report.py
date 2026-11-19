"""Report layouts (per company) and versioned templates."""

from __future__ import annotations

import base64
from typing import Any

from tessera.exceptions import AccessError, UserError, ValidationError
from tessera.i18n import _
from tessera.jobs import job
from tessera.orm import Model, api, fields
from tessera.reports import blocks_to_template, render_html, render_pdf


class Layout(Model):
    _name = "reports.layout"
    _description = "Report layout"
    _order = "company_id, name"
    _audit = True

    name = fields.Char(required=True)
    company_id = fields.Many2one("core.company", ondelete="cascade", help="Empty = default for every company")
    paper = fields.Selection(
        [("A4", "A4"), ("Letter", "US Letter"), ("Legal", "US Legal"), ("A5", "A5")], default="A4", required=True
    )
    orientation = fields.Selection(
        [("portrait", "Portrait"), ("landscape", "Landscape")], default="portrait", required=True
    )
    margin = fields.Char(default="18mm 15mm 20mm 15mm", help="CSS margins: top right bottom left")
    primary_color = fields.Char(default="#1f4e79")
    font = fields.Char()
    logo = fields.Binary(attachment=True)
    header_html = fields.Text(help="Template: e.g. {{ company.name }}")
    footer_html = fields.Text(help="Template: e.g. {{ company.email }} · {{ company.phone }}")
    extra_css = fields.Text()
    active = fields.Boolean(default=True)

    def as_dict(self) -> dict[str, Any]:
        self.ensure_one()
        logo = self.elevated().logo
        return {
            "paper": self.paper,
            "orientation": self.orientation,
            "margin": self.margin,
            "primary_color": self.primary_color,
            "font": self.font,
            "header_html": self.header_html,
            "footer_html": self.footer_html,
            "extra_css": self.extra_css,
            "logo_data_uri": f"data:image/png;base64,{base64.b64encode(logo).decode()}" if logo else None,
        }


class Template(Model):
    _name = "reports.template"
    _description = "Report template"
    _order = "model_name, name"
    _audit = True

    name = fields.Char(required=True, translate=True)
    model_name = fields.Char("Model", required=True, index=True)
    filename = fields.Char(default="report", help="Template for the file name, e.g. Quote {{ doc.name }}")
    layout_id = fields.Many2one("reports.layout", ondelete="set_null", help="Empty = the company's layout")
    version_ids = fields.One2many("reports.template_version", "template_id", "Versions")
    current_version_id = fields.Many2one("reports.template_version", ondelete="set_null", readonly=True)
    group_ids = fields.Many2many(
        "core.group",
        relation="reports_template__groups",
        column1="template_id",
        column2="group_id",
        help="Empty = everyone who can read the records",
    )
    active = fields.Boolean(default=True)

    @api.constrains("model_name")
    def _check_model(self) -> None:
        for tpl in self:
            if tpl.model_name not in self.env.registry.models:
                raise ValidationError(_("Unknown model '{model_name}'", model_name=tpl.model_name), field="model_name")

    @api.expose
    def publish_version(
        self, body: str | None = None, blocks: list[dict[str, Any]] | None = None, note: str | None = None
    ) -> dict[str, Any]:
        """Save a new version (from a template body or editor blocks) and make it current."""
        self.ensure_one()
        if not body and not blocks:
            raise UserError(_("Give a template body or blocks"))
        source = body or blocks_to_template(blocks or [])
        from tessera.reports.engine import _ENV

        try:
            _ENV.parse(source)
        except Exception as exc:
            raise ValidationError(_("Template error: {exc}", exc=exc), field="body") from exc
        number = (max(self.version_ids.mapped("number")) if self.version_ids else 0) + 1
        version = self.env["reports.template_version"].create(
            {"template_id": self.ids[0], "number": number, "body": source, "blocks": blocks, "note": note}
        )
        self.write({"current_version_id": version.ids[0]})
        return {"version": number, "id": version.ids[0]}

    @api.expose
    def rollback_to(self, number: int) -> dict[str, Any]:
        self.ensure_one()
        version = self.version_ids.filtered(lambda v: v.number == int(number))
        if not version:
            raise UserError(_("No version {number}", number=number))
        self.write({"current_version_id": version.ids[0]})
        return {"version": int(number)}

    def _layout_for(self, records: Model) -> Model:
        if self.layout_id:
            return self.layout_id
        layouts = self.env["reports.layout"].elevated().search([("active", "=", True)])
        company = records[:1].company_id if "company_id" in records._fields and records else self.env.company
        cid = company.ids[0] if company else None
        return next((lay for lay in layouts if lay.company_id and lay.company_id.ids[0] == cid), None) or next(
            (lay for lay in layouts if not lay.company_id), layouts.browse([])
        )

    def render(self, res_ids: list[int], fmt: str = "pdf") -> tuple[bytes, str, str]:
        """Render as the current user: (content, mimetype, filename)."""
        self.ensure_one()
        if self.group_ids:
            xmlids = [x for x in self.group_ids.get_external_id().values() if x]
            if not self.env.registry.security.has_groups(self.env, xmlids):
                raise AccessError(_("You are not allowed to print this report"))
        version = self.elevated().current_version_id
        if not version:
            raise UserError(_("Report '{name}' has no published version", name=self.name))
        records = self.env[self.model_name].browse(res_ids)
        records.check_access("read")
        layout = self._layout_for(records)
        company = (
            records[:1].company_id
            if "company_id" in records._fields and records and records[:1].company_id
            else self.env.company
        )
        from tessera.reports.engine import wrap

        context = {
            "docs": list(records),
            "company": wrap(company.elevated()),
            "user": wrap(self.env.user.elevated()),
            "lang": self.env.lang,
        }
        html_doc = render_html(version.body, context, layout=layout.as_dict() if layout else None, title=self.name)
        from tessera.reports.engine import _ENV

        first = records[:1]
        name = (
            _ENV.from_string(self.filename or "report").render(doc=wrap(first), company=context["company"])
            if first
            else "report"
        )
        safe = "".join(ch for ch in name if ch.isalnum() or ch in " -_.").strip() or "report"
        if fmt == "html":
            return html_doc.encode(), "text/html; charset=utf-8", f"{safe}.html"
        return render_pdf(html_doc), "application/pdf", f"{safe}.pdf"


class TemplateVersion(Model):
    _name = "reports.template_version"
    _description = "Report template version"
    _order = "template_id, number desc"
    _rec_name = "number"

    template_id = fields.Many2one("reports.template", required=True, ondelete="cascade", index=True)
    number = fields.Integer(required=True)
    body = fields.Text(required=True)
    blocks = fields.Json()
    note = fields.Char()

    _sql_constraints = [("number_uniq", "UNIQUE (template_id, number)", "Version numbers are unique per template")]


@job("reports.render", queue="reports", max_attempts=2, timeout_seconds=300)
def render_job(env: Any, template_id: int, res_ids: list[int], fmt: str = "pdf") -> dict[str, Any]:
    """Render in the worker and store the file as an attachment on the first record."""
    tpl = env["reports.template"].browse([template_id])
    content, mimetype, filename = tpl.render(res_ids, fmt)
    att = env["core.attachment"].create(
        {
            "name": filename,
            "mimetype": mimetype.split(";")[0],
            "model": tpl.model_name,
            "res_id": res_ids[0] if res_ids else None,
            "raw": content,
        }
    )
    return {"attachment_id": att.ids[0], "filename": filename, "size": len(content)}
