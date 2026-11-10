"""Attachments: metadata in the database, content in object storage (content-addressed)."""

from __future__ import annotations

import mimetypes
import secrets
from typing import Any

from tessera.exceptions import AccessError, MissingError
from tessera.i18n import _
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

    @api.expose
    def generate_access_token(self) -> str:
        self.ensure_one()
        self.check_record_access("read")
        token = self.elevated().access_token
        if not token:
            token = secrets.token_urlsafe(32)
            self.elevated().write({"access_token": token})
        return str(token)

    # -- object-level access -------------------------------------------------------------------
    # Attachments carry no record rule: who may see one depends on the record it is linked to. These
    # overrides apply that check on every ORM path (REST, JSON-RPC, exposed methods), not only in the
    # download endpoint.

    def check_record_access(self, operation: str = "read") -> None:
        """An attachment is readable if its linked record is readable (or it is public), and may be
        changed or deleted if the linked record may be written. Creating one needs read access on the
        record it is attached to. An attachment linked to no record belongs to whoever uploaded it."""
        if self.env.is_system:
            return
        target_op = "read" if operation in ("read", "create") else "write"
        for att in self.elevated("attachment access"):
            if att.public and operation == "read":
                continue
            model_name, res_id = att.model, att.res_id
            if model_name and res_id:
                if model_name not in self.env:
                    raise AccessError(_("You are not allowed to access this attachment"), attachment=att.ids[0])
                try:
                    self.env[model_name].browse([res_id]).check_access(target_op)
                except (AccessError, MissingError) as exc:
                    raise AccessError(
                        _("You are not allowed to access this attachment"), attachment=att.ids[0]
                    ) from exc
            elif att.create_uid.ids != [self.env.uid]:
                raise AccessError(_("You are not allowed to access this attachment"), attachment=att.ids[0])

    def check_access_rule(self, operation: str) -> None:
        super().check_access_rule(operation)
        self.check_record_access(operation)

    def read(self, fields: Any = None, load: str = "display") -> list[dict[str, Any]]:
        self.check_record_access("read")
        return super().read(fields, load)

    def search(self, domain: Any = None, offset: int = 0, limit: int | None = None, order: str | None = None) -> Model:
        found = super().search(domain, offset=offset, limit=limit, order=order)
        if self.env.is_system or not found:
            return found
        return found.browse(found._readable_ids())

    def _readable_ids(self) -> list[int]:
        """The ids of ``self`` the current user may read (one query per linked model)."""
        uid = self.env.uid
        rows = self.elevated("attachment access").read(["public", "model", "res_id", "create_uid"], load="raw")
        by_model: dict[str, set[int]] = {}
        for row in rows:
            if row["model"] and row["res_id"] and not row["public"]:
                by_model.setdefault(row["model"], set()).add(row["res_id"])
        visible: dict[str, set[int]] = {}
        for model_name, res_ids in by_model.items():
            if model_name not in self.env:
                visible[model_name] = set()
                continue
            target = self.env[model_name].with_context(active_test=False)
            if not target.check_access_rights("read", raise_exception=False):
                visible[model_name] = set()
                continue
            visible[model_name] = set(target.search([("id", "in", sorted(res_ids))]).ids)
        out = []
        for row in rows:
            if row["public"]:
                out.append(row["id"])
            elif row["model"] and row["res_id"]:
                if row["res_id"] in visible.get(row["model"], set()):
                    out.append(row["id"])
            elif row["create_uid"] == uid:
                out.append(row["id"])
        return out

    @api.expose
    def action_download(self) -> dict[str, object]:
        self.ensure_one()
        self.check_record_access("read")
        return {"kind": "url", "url": f"/api/v1/attachments/{self.ids[0]}/content", "target": "download"}
