"""AI anomaly flags on invoices and bills (``account.anomaly``) and the AI fields of ``account.move``.

A flag is a notification-level finding of the detector in ``modules.account.ai_anomaly`` (possible
duplicate, unusual amount, unexpected vendor, changed bank account) with its explanation. Flags never
block posting; a person dismisses or confirms them.
"""

from __future__ import annotations

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields

ANOMALY_KINDS = [
    ("duplicate_number", "Same invoice number"),
    ("duplicate_amount", "Same amount and date"),
    ("near_duplicate_text", "Nearly identical document"),
    ("unusual_amount", "Unusual amount for this vendor"),
    ("new_vendor_large", "First bill of a new vendor, large amount"),
    ("bank_account_changed", "Bank account changed recently"),
]
SEVERITIES = [("low", "Low"), ("medium", "Medium"), ("high", "High")]


class MoveAnomaly(Model):
    _name = "account.move_anomaly"
    _description = "Anomaly flag"
    _order = "id desc"
    _rec_name = "kind"

    move_id = fields.Many2one("account.move", "Document", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="move_id.company_id", store=True, index=True)
    partner_id = fields.Many2one("contacts.partner", related="move_id.partner_id", store=True, string="Partner")
    kind = fields.Selection(ANOMALY_KINDS, required=True, index=True)
    severity = fields.Selection(SEVERITIES, default="medium", required=True)
    score = fields.Float(digits=(16, 3), help="Strength of the signal (robust z-score, similarity…)")
    explanation = fields.Text(required=True)
    related_move_id = fields.Many2one("account.move", "Similar document", ondelete="set_null")
    details = fields.Json(default=dict)
    state = fields.Selection(
        [("open", "Open"), ("dismissed", "Dismissed"), ("confirmed", "Confirmed")],
        default="open",
        required=True,
        index=True,
        tracking=False,
    )
    decided_by = fields.Many2one("core.user", readonly=True)
    decided_at = fields.Datetime(readonly=True)
    note = fields.Char("Reason")
    ai_generated = fields.Boolean(default=True, readonly=True)

    @api.depends("kind", "move_id")
    def _compute_display_name(self) -> None:
        labels = dict(ANOMALY_KINDS)
        for flag in self:
            flag.display_name = _(labels.get(flag.kind or "", flag.kind or ""))

    def _decide(self, state: str, note: str | None) -> bool:
        for flag in self:
            if flag.state != "open":
                raise UserError(_("This flag is already {state}", state=flag.state))
        self.check_access("write")
        self.elevated("decide anomaly flag").write(
            {
                "state": state,
                "note": (note or "")[:250] or None,
                "decided_by": self.env.uid,
                "decided_at": self.env.now(),
            }
        )
        return True

    @api.expose
    def action_dismiss(self, note: str | None = None) -> bool:
        """Not an anomaly (the detector will not raise it again for the same document)."""
        return self._decide("dismissed", note)

    @api.expose
    def action_confirm(self, note: str | None = None) -> bool:
        """A real problem (kept for the audit trail; the document is unchanged)."""
        return self._decide("confirmed", note)


class MoveAi(Model):
    _extend = "account.move"

    ai_ocr_text = fields.Text("Document text", copy=False, readonly=True, help="Text read from the source document")
    ai_anomaly_ids = fields.One2many("account.move_anomaly", "move_id", "Anomaly flags")
    ai_anomaly_count = fields.Integer("Open anomaly flags", compute="_compute_ai_anomaly_count")

    def _compute_ai_anomaly_count(self) -> None:
        counts: dict[int, int] = {}
        if self.ids:
            rows = self.env.cr.fetchall(
                """SELECT move_id, COUNT(*) FROM account_move_anomaly
                   WHERE move_id = ANY(:ids) AND state = 'open' GROUP BY move_id""",
                {"ids": list(self.ids)},
            )
            counts = {r[0]: r[1] for r in rows}
        for move in self:
            move.ai_anomaly_count = counts.get(move.ids[0], 0) if move.ids else 0
