"""AI configuration edited by administrators: providers, routes, budgets, company settings, features,
flags. The gateway reads these tables directly (``gateway.config.SqlConfigStore``), so column names
are a contract with the gateway service; change them together."""

from __future__ import annotations

from typing import Any

from tessera.exceptions import ValidationError
from tessera.orm import Model, api, fields
from tessera.security.crypto import encrypt, is_encrypted

PROVIDER_KINDS = [
    ("anthropic", "Anthropic"),
    ("openai", "OpenAI"),
    ("azure_openai", "Azure OpenAI"),
    ("google", "Google Gemini"),
    ("openai_compatible", "Self-hosted (vLLM / Ollama, OpenAI-compatible)"),
    ("voyage", "Voyage AI (embeddings)"),
    ("mock", "Deterministic test provider"),
]
TASK_CLASSES = [
    ("classify", "Classify"),
    ("extract", "Extract"),
    ("embed", "Embed"),
    ("summarize", "Summarize"),
    ("draft", "Draft"),
    ("reason", "Reason"),
    ("translate", "Translate"),
    ("nl_query", "Natural language to query"),
    ("vision", "Vision"),
    ("transcribe", "Transcribe"),
]
RISK_CLASSES = [("minimal", "Minimal"), ("limited", "Limited"), ("high", "High (employment)")]


class Provider(Model):
    _name = "ai_core.provider"
    _description = "AI provider"
    _order = "name"

    name = fields.Char(required=True)
    kind = fields.Selection(PROVIDER_KINDS, required=True, default="anthropic")
    api_key = fields.Char(
        "API key",
        groups="core.group_system",
        copy=False,
        help="Stored encrypted; empty → the gateway's environment key",
    )
    base_url = fields.Char("Endpoint", help="Custom endpoint: Azure resource, Vertex region, self-hosted server")
    region = fields.Char(help="Residency label (us, eu, self-hosted…), matched against each company's allowed regions")
    approved_for_pii = fields.Boolean(
        "Approved for personal data",
        help="Features that must send names or contact details only use approved providers",
    )
    retention = fields.Char(help="The provider's data-retention regime (shown to administrators)")
    options = fields.Json(
        default=dict, help="Adapter options: inference_geo, deployments, json_mode, pricing, timeout…"
    )
    active = fields.Boolean(default=True)

    def _encrypt_key(self, vals: dict[str, Any]) -> dict[str, Any]:
        key = vals.get("api_key")
        if key and not is_encrypted(key):
            vals = {**vals, "api_key": encrypt(key)}
        return vals

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        return super().create([self._encrypt_key(v) for v in batch])

    def write(self, vals):  # type: ignore[no-untyped-def]
        return super().write(self._encrypt_key(dict(vals)))


class Route(Model):
    _name = "ai_core.route"
    _description = "AI routing rule"
    _order = "company_id, task_class"

    company_id = fields.Many2one("core.company", index=True, help="Empty → default for every company")
    task_class = fields.Selection(TASK_CLASSES, required=True)
    provider_id = fields.Many2one("ai_core.provider", "Provider", required=True, ondelete="cascade")
    model = fields.Char(required=True)
    params = fields.Json(default=dict, help="effort, max_output_tokens…")
    fallback_provider_id = fields.Many2one("ai_core.provider", "Fallback provider", ondelete="set_null")
    fallback_model = fields.Char()
    fallback_params = fields.Json(default=dict)
    active = fields.Boolean(default=True)

    @api.constrains("fallback_provider_id", "fallback_model")
    def _check_fallback(self) -> None:
        for r in self:
            if r.fallback_provider_id and not r.fallback_model:
                raise ValidationError("A fallback provider needs a model", field="fallback_model")


class Budget(Model):
    _name = "ai_core.budget"
    _description = "AI budget or quota"

    company_id = fields.Many2one("core.company", required=True, index=True)
    user_id = fields.Many2one("core.user", "User quota", help="Set → a per-user quota inside the company")
    feature = fields.Char(help="Set → a cap for one feature")
    period = fields.Selection([("day", "Day"), ("month", "Month")], default="month", required=True)
    limit_cost = fields.Float("Spend limit (USD)", digits=(14, 6))
    limit_tokens = fields.Integer()
    limit_requests = fields.Integer()
    hard = fields.Boolean(
        default=True, help="Hard: calls are refused beyond the limit. Soft: administrators are alerted"
    )
    alert_pct = fields.Integer("Alert at %", default=80)
    active = fields.Boolean(default=True)


class CompanySetting(Model):
    _name = "ai_core.company_setting"
    _description = "AI settings per company"

    company_id = fields.Many2one("core.company", index=True, help="Empty → the global switch")
    ai_enabled = fields.Boolean("AI enabled", default=True)
    allowed_regions = fields.Json(default=list, help="Provider regions this company accepts; empty → any")
    retention_days = fields.Integer(default=90, help="How long prompt inputs and outputs of runs are kept")

    _sql_constraints = [("company_unique", "UNIQUE (company_id)", "One AI settings row per company")]


class Feature(Model):
    _name = "ai_core.feature"
    _description = "AI feature"
    _order = "module, name"
    _rec_name = "title"

    name = fields.Char(required=True, index="unique")
    title = fields.Char(required=True)
    module = fields.Char(required=True)
    description = fields.Text()
    task_class = fields.Selection(TASK_CLASSES, required=True)
    prompt = fields.Char(help="Prompt registry name")
    tools = fields.Json(default=list, help="Whitelisted tool names")
    writes = fields.Boolean(help="Produces write proposals")
    outbound = fields.Boolean(help="Produces outbound message proposals")
    redaction = fields.Selection([("redact", "Redact"), ("minimal", "Minimal"), ("none", "None")], default="redact")
    risk_class = fields.Selection(RISK_CLASSES, default="minimal", required=True)
    employment_related = fields.Boolean()
    human_decision = fields.Boolean(help="Output is informational; a named person must make the decision")
    bias_tests = fields.Char(help="Link to bias-test results (required for high-risk employment features)")
    interactive = fields.Boolean(default=True)
    disclosure = fields.Selection(
        [("ai_drafted", "AI-drafted label"), ("ai_generated", "AI-generated label")], default="ai_drafted"
    )
    cache_ttl = fields.Integer(help="Seconds a response may be served from cache (0 = never)")
    max_steps = fields.Integer(default=4, help="Upper bound of model calls in the tool loop")
    eval_set = fields.Char()
    min_eval_cases = fields.Integer(default=30)
    thresholds = fields.Json(default=dict)
    flag_default = fields.Boolean("Enabled by default", default=True)
    flag_ids = fields.One2many("ai_core.feature_flag", "feature_id", "Flags")

    def enabled_for(self, company_id: int | None) -> bool:
        """Runtime kill-switch check (the gateway checks the same flags independently)."""
        self.ensure_one()
        me = self.elevated()
        settings = self.env["ai_core.company_setting"].elevated()
        glob = settings.search([("company_id", "=", False)], limit=1)
        if glob and not glob.ai_enabled:
            return False
        if company_id:
            own = settings.search([("company_id", "=", company_id)], limit=1)
            if own and not own.ai_enabled:
                return False
        flags = {f.company_id.ids[0] if f.company_id else None: f.enabled for f in me.flag_ids}
        if None in flags and not flags[None]:
            return False
        return bool(flags.get(company_id, flags.get(None, me.flag_default)))

    def set_enabled(self, company_id: int | None, enabled: bool) -> None:
        self.ensure_one()
        flags = self.env["ai_core.feature_flag"].elevated()
        existing = flags.search([("feature_id", "=", self.ids[0]), ("company_id", "=", company_id or False)], limit=1)
        if existing:
            existing.write({"enabled": enabled})
        else:
            flags.create({"feature_id": self.ids[0], "company_id": company_id, "enabled": enabled})


class FeatureFlag(Model):
    _name = "ai_core.feature_flag"
    _description = "AI feature flag / kill switch"

    feature_id = fields.Many2one("ai_core.feature", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", help="Empty → applies to every company")
    enabled = fields.Boolean(default=True)

    _sql_constraints = [
        ("feature_company_unique", "UNIQUE NULLS NOT DISTINCT (feature_id, company_id)", "One flag per company")
    ]
