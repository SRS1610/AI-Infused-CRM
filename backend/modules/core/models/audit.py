from __future__ import annotations

from tessera.orm import Model, fields


class AuditLog(Model):
    """Who changed what and when, for models declaring ``_audit = True``."""

    _name = "core.audit_log"
    _description = "Audit log entry"
    _order = "id desc"
    _rec_name = "model"
    _log_access = False
    _studio = False
    _indexes = [("model_res_id", ["model", "res_id"]), ("user_timestamp", ["user_id", "timestamp"])]

    model = fields.Char(required=True, index=True)
    res_id = fields.Integer(required=True)
    operation = fields.Selection(
        [
            ("create", "Create"),
            ("write", "Update"),
            ("unlink", "Delete"),
            ("restore", "Restore"),
            ("read", "Read (PII)"),
        ],
        required=True,
    )
    user_id = fields.Many2one("core.user", index=True, ondelete="set_null", fk=False)
    company_id = fields.Many2one("core.company", ondelete="set_null", fk=False)
    changes = fields.Json(help='{"field": {"old": ..., "new": ...}}')
    request_id = fields.Char(index=True)
    ip = fields.Char()
    ai_run_id = fields.Integer(help="Set when the change came from an accepted AI proposal")
    timestamp = fields.Datetime(required=True, default=lambda self: self.env.now(), index=True)


class PiiAccessLog(Model):
    _name = "core.pii_access_log"
    _description = "PII access log"
    _order = "id desc"
    _rec_name = "model"
    _log_access = False
    _studio = False

    model = fields.Char(required=True, index=True)
    res_ids = fields.Json(required=True)
    fields_read = fields.Json(required=True)
    user_id = fields.Many2one("core.user", index=True, ondelete="set_null", fk=False)
    request_id = fields.Char(index=True)
    timestamp = fields.Datetime(required=True, default=lambda self: self.env.now(), index=True)
