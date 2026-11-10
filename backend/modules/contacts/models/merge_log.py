from __future__ import annotations

from typing import Any

from tessera.orm import Model, api, fields


class MergeLog(Model):
    _name = "contacts.merge_log"
    _description = "Contact merge"
    _order = "id desc"
    _rec_name = "kept_partner_id"

    kept_partner_id = fields.Many2one("contacts.partner", "Kept contact", required=True, ondelete="cascade")
    merged_partner_ids = fields.Json(default=list)
    field_choices = fields.Json(default=dict)
    relinked = fields.Json(default=dict, help="Every reference moved, so the merge can be undone")
    method = fields.Selection(
        [("manual", "Manual"), ("rule", "Rule"), ("ai_suggested", "AI suggestion")], default="manual", required=True
    )
    ai_run_id = fields.Integer()
    undone = fields.Boolean(readonly=True)
    undone_at = fields.Datetime(readonly=True)

    @api.expose
    def action_undo(self) -> dict[str, Any]:
        from modules.contacts.merge import undo_merge

        self.ensure_one()
        self.kept_partner_id.check_access("write")
        undo_merge(self.env, self)
        return {"undone": True}
