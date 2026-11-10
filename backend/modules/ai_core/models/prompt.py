"""Prompt registry: versioned, parameterised prompts loaded from ``modules/<m>/ai/prompts/*.yaml``.

Status flow ``draft → testing → active → retired``; one active version per prompt (companies may pin
another); ``testing`` versions are served to named testers or a rollout percentage.
"""

from __future__ import annotations

import hashlib
from typing import Any

from tessera.exceptions import UserError
from tessera.orm import Model, api, fields

STATUSES = [("draft", "Draft"), ("testing", "Testing"), ("active", "Active"), ("retired", "Retired")]


class Prompt(Model):
    _name = "ai_core.prompt"
    _description = "AI prompt"
    _order = "name"

    name = fields.Char(required=True, index="unique")
    module = fields.Char()
    description = fields.Text()
    version_ids = fields.One2many("ai_core.prompt_version", "prompt_id", "Versions")

    def version_for(self, company_id: int | None, user_id: int | None) -> Model:
        """The version a given user of a company is served."""
        self.ensure_one()
        versions = self.env["ai_core.prompt_version"].elevated()
        if company_id:
            pin = (
                self.env["ai_core.prompt_pin"]
                .elevated()
                .search([("prompt_id", "=", self.ids[0]), ("company_id", "=", company_id)], limit=1)
            )
            if pin:
                return pin.version_id
        testing = versions.search([("prompt_id", "=", self.ids[0]), ("status", "=", "testing")], order="version desc")
        for v in testing:
            if user_id and user_id in v.tester_ids.ids:
                return v
            if v.rollout_pct and user_id is not None:
                bucket = int(hashlib.sha256(f"{v.ids[0]}:{user_id}".encode()).hexdigest(), 16) % 100
                if bucket < v.rollout_pct:
                    return v
        active = versions.search([("prompt_id", "=", self.ids[0]), ("status", "=", "active")], limit=1)
        if not active:
            raise UserError(f"Prompt '{self.name}' has no active version")
        return active


class PromptVersion(Model):
    _name = "ai_core.prompt_version"
    _description = "AI prompt version"
    _order = "prompt_id, version desc"
    _rec_name = "ref"

    prompt_id = fields.Many2one("ai_core.prompt", required=True, ondelete="cascade", index=True)
    version = fields.Integer(required=True)
    ref = fields.Char(compute="_compute_ref", store=True, help="name@version, recorded on every run")
    status = fields.Selection(STATUSES, default="draft", required=True)
    task_class = fields.Char()
    system = fields.Text(required=True)
    template = fields.Text(required=True)
    input_schema = fields.Json(default=dict)
    output_schema = fields.Json()
    model_hints = fields.Json(default=dict)
    test_inputs = fields.Json(default=list)
    content_hash = fields.Char(index=True)
    note = fields.Char()
    rollout_pct = fields.Integer("Rollout %", help="Share of users served this testing version")
    tester_ids = fields.Many2many("core.user", string="Testers")

    _sql_constraints = [
        ("prompt_version_unique", "UNIQUE (prompt_id, version)", "Version numbers are unique per prompt")
    ]

    @api.depends("prompt_id.name", "version")
    def _compute_ref(self) -> None:
        for v in self:
            v.ref = f"{v.prompt_id.name}@{v.version}" if v.prompt_id else ""

    def activate(self) -> None:
        """Make this the active version (retiring the previous one). Versions are immutable once active."""
        self.ensure_one()
        others = self.search(
            [("prompt_id", "=", self.prompt_id.ids[0]), ("status", "=", "active"), ("id", "!=", self.ids[0])]
        )
        others.write({"status": "retired"})
        self.write({"status": "active"})

    def write(self, vals):  # type: ignore[no-untyped-def]
        content = {"system", "template", "input_schema", "output_schema"} & set(vals)
        if content and any(v.status in ("active", "retired") for v in self):
            raise UserError("Active and retired prompt versions are immutable; create a new version")
        return super().write(vals)


class PromptPin(Model):
    _name = "ai_core.prompt_pin"
    _description = "Prompt version pinned for a company"

    prompt_id = fields.Many2one("ai_core.prompt", required=True, ondelete="cascade")
    company_id = fields.Many2one("core.company", required=True)
    version_id = fields.Many2one("ai_core.prompt_version", required=True, ondelete="cascade")

    _sql_constraints = [("pin_unique", "UNIQUE (prompt_id, company_id)", "One pinned version per company")]


def content_hash(data: dict[str, Any]) -> str:
    import json

    keys = ("system", "template", "input_schema", "output_schema")
    return hashlib.sha256(json.dumps({k: data.get(k) for k in keys}, sort_keys=True).encode()).hexdigest()
