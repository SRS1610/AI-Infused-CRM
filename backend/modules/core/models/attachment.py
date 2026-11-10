"""Attachments: metadata in the database, content in object storage (content-addressed)."""

from __future__ import annotations

import mimetypes
import secrets

from tessera.exceptions import AccessError
from tessera.orm import Model, api, fields
from tessera.storage import content_key, get_storage


class Attachment(Model):
    _name = "core.attachment"
    _description = "Attachment"
    _order = "id desc"
    _indexes = [("model_res_id", ["model", "res_id"])]

    name = fields.Char(required=True, index=True)
    description = fields.Text()
    model = fields.Char("Record model", index=True)
    res_id = fields.Integer("Record id", index=True)
    res_field = fields.Char("Record field")
    mimetype = fields.Char()
    file_size = fields.Integer(readonly=True)
    checksum = fields.Char(readonly=True, index=True)
    store_key = fields.Char(readonly=True, groups="core.group_system")
    public = fields.Boolean(default=False, help="Readable without authentication (e.g. company logo)")
    access_token = fields.Char(copy=False, groups="core.group_system")
    company_id = fields.Many2one(
        "core.company", index=True, default=lambda self: self.env.company.ids[0] if self.env.company else None
    )
    raw = fields.Binary(compute="_compute_raw", inverse="_inverse_raw", store=False)
    url = fields.Char(help="External link instead of stored content")
    type = fields.Selection([("binary", "File"), ("url", "Link")], default="binary", required=True)

    def _compute_raw(self) -> None:
        storage = get_storage(self.env.cr.dbname)
        field = self._fields["raw"]
        for att in self:
            key = att.elevated("read attachment").store_key
            data = storage.get(key) if key and storage.exists(key) else None
            self.env.cache.set(field, att.ids[0], data, att._cache_qualifier(field))

    def _inverse_raw(self) -> None:
        storage = get_storage(self.env.cr.dbname)
        field = self._fields["raw"]
        for att in self:
            data = self.env.cache.get(field, att.ids[0], att._cache_qualifier(field), default=None)
            if data is None:
                att.elevated().with_context(_recompute_write=True)._write_columns(
                    {"store_key": None, "checksum": None, "file_size": 0}
                )
                continue
            checksum, key = content_key(data)
            storage.put(key, data)
            vals = {"store_key": key, "checksum": checksum, "file_size": len(data)}
            if not att.mimetype:
                vals["mimetype"] = mimetypes.guess_type(att.name or "")[0] or "application/octet-stream"
            att.elevated().with_context(_recompute_write=True)._write_columns(vals)

    def generate_access_token(self) -> str:
        self.ensure_one()
        token = self.elevated().access_token
        if not token:
            token = secrets.token_urlsafe(32)
            self.elevated().write({"access_token": token})
        return str(token)

    def check_record_access(self, operation: str = "read") -> None:
        """An attachment is readable/writable if its linked record is."""
        for att in self:
            if self.env.is_system or (att.public and operation == "read"):
                continue
            model_name, res_id = att.elevated().model, att.elevated().res_id
            if model_name and res_id and model_name in self.env:
                try:
                    self.env[model_name].browse([res_id]).check_access(operation)
                except AccessError as exc:
                    raise AccessError("You are not allowed to access this attachment", attachment=att.ids[0]) from exc

    @api.expose
    def action_download(self) -> dict[str, object]:
        self.ensure_one()
        self.check_record_access("read")
        return {"kind": "url", "url": f"/api/v1/attachments/{self.ids[0]}/content", "target": "download"}
