"""Churn prediction storage (sub-phase 2.6): the score on each running subscription and the company's trained models.

The scores are written by the daily scoring job (as the system) or on request; nothing else changes. They drive the
churn panel of the subscription form and the *At risk* filter. See ``modules.sale_subscription.ai_features``.
"""

from __future__ import annotations

from tessera.orm import Model, fields

CHURN_RISK = [("low", "Low"), ("medium", "Medium"), ("high", "High")]


class ChurnModel(Model):
    _name = "sale.subscription_churn_model"
    _description = "Churn prediction model (AI)"
    _order = "company_id, trained_at desc, id desc"
    _studio = False

    name = fields.Char(required=True)
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", index=True)
    active = fields.Boolean(default=True)
    algorithm = fields.Char(required=True)
    artefact = fields.Json(help="Standardisation and coefficients (JSON, no pickle)")
    artefact_hash = fields.Char(help="SHA-256 of the artefact, checked before scoring")
    metrics = fields.Json(default=dict, help="Training rows, churned, base rate, hold-out AUC")
    trained_at = fields.Datetime()


class QuotationChurn(Model):
    _extend = "sale.quotation"

    churn_probability = fields.Float("Churn probability", digits=(5, 4), readonly=True, copy=False)
    churn_score = fields.Integer("Churn score", readonly=True, copy=False, help="0-100, from the churn prediction")
    churn_risk = fields.Selection(CHURN_RISK, "Churn risk", readonly=True, copy=False, index=True)
    churn_factors = fields.Json(readonly=True, copy=False, help="Top contributing factors: feature, direction, weight")
    churn_facts = fields.Json(readonly=True, copy=False, help="The churn facts the score was computed from")
    churn_scored_at = fields.Datetime("Churn scored on", readonly=True, copy=False)
    churn_model_id = fields.Many2one(
        "sale.subscription_churn_model", "Churn model", ondelete="set_null", readonly=True, copy=False
    )
