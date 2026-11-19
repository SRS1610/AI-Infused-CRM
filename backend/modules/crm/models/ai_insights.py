"""CRM insights (Phase 1.6): per-company scoring models, AI fields on leads, stored forecasts and the
routing auto-apply switch. Written by system jobs and labelled AI in the UI; never edited by hand."""

from __future__ import annotations

from tessera.orm import Model, fields

SCORE_SOURCES = [("model", "Predictive model"), ("rules", "Scoring rules (not enough history yet)")]
RISK_LEVELS = [("none", "No risk signals"), ("low", "Low"), ("medium", "At risk"), ("high", "High risk")]


class ScoreModel(Model):
    _name = "crm.ai_score_model"
    _description = "Lead scoring model (AI)"
    _order = "company_id, version desc"
    _rec_name = "display_label"
    _studio = False

    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", index=True)
    version = fields.Integer(required=True)
    state = fields.Selection([("active", "Active"), ("retired", "Retired")], default="active", required=True)
    algorithm = fields.Char(required=True)
    feature_names = fields.Json(default=list, help="Model inputs (none of them a protected attribute)")
    artefact = fields.Json(help="Serialised model: standardisation, coefficients, vocabulary (JSON, no pickle)")
    artefact_hash = fields.Char(help="SHA-256 of the artefact, checked before scoring")
    metrics = fields.Json(default=dict, help="Training rows, won/lost, base rate, hold-out AUC")
    trained_at = fields.Datetime()
    display_label = fields.Char(compute="_compute_label")

    def _compute_label(self) -> None:
        for m in self:
            m.display_label = f"Lead scoring v{m.version}"


class LeadInsights(Model):
    _extend = "crm.lead"

    ai_score = fields.Integer("AI score", readonly=True, index=True, help="0-100, written by the scoring job")
    ai_probability = fields.Float("AI win probability", readonly=True, digits=(5, 4))
    ai_score_factors = fields.Json(readonly=True, help="Top contributing factors: feature, direction, weight")
    ai_score_source = fields.Selection(SCORE_SOURCES, readonly=True)
    ai_score_model_id = fields.Many2one("crm.ai_score_model", readonly=True, ondelete="set_null")
    ai_scored_at = fields.Datetime(readonly=True, copy=False)
    ai_risk_level = fields.Selection(RISK_LEVELS, readonly=True, copy=False)
    ai_risk_score = fields.Float(readonly=True, copy=False)
    ai_risk_reasons = fields.Json(readonly=True, copy=False)
    ai_risk_checked_at = fields.Datetime(readonly=True, copy=False)


class Forecast(Model):
    _name = "crm.ai_forecast"
    _description = "Sales forecast (AI)"
    _order = "id desc"
    _rec_name = "name"
    _studio = False

    name = fields.Char(required=True)
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", index=True)
    team_id = fields.Many2one("crm.team", ondelete="cascade")
    user_id = fields.Many2one("core.user", "Requested by", ondelete="set_null")
    periods = fields.Json(default=list, help="[{period, label, p10, p50, p90, explanation, components, drivers}]")
    method = fields.Char()
    narrative = fields.Text(help="Optional AI narration of the figures (the figures never come from it)")
    run_id = fields.Integer(help="ai_core.run of this forecast (audit trail, feedback)")
    generated_at = fields.Datetime()


class AssignmentRuleAi(Model):
    _extend = "crm.assignment_rule"

    ai_auto_apply = fields.Boolean(
        "Apply AI routing without review",
        default=False,
        help="When an AI routing suggestion follows this rule, assign the lead directly instead of proposing "
        "it. Off by default: every suggestion is a proposal a person accepts or rejects.",
    )
