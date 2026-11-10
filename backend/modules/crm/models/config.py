"""Pipeline configuration: stages, tags, lost reasons, marketing origin, teams, scoring and assignment rules."""

from __future__ import annotations

import secrets
from typing import Any

from tessera.exceptions import ValidationError
from tessera.orm import Model, api, fields
from tessera.orm.domain import Domain


class Stage(Model):
    _name = "crm.stage"
    _description = "Pipeline stage"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    is_won = fields.Boolean("Won stage")
    probability = fields.Float(default=10.0, help="Default probability (%) of opportunities entering this stage")
    fold = fields.Boolean(help="Folded in the kanban view")
    requirements = fields.Text(help="What must be true before moving an opportunity here")
    team_ids = fields.Many2many(
        "crm.team", relation="crm_stage__team_ids", column1="stage_id", column2="team_id", help="Empty = every team"
    )
    active = fields.Boolean(default=True)


class LostReason(Model):
    _name = "crm.lost_reason"
    _description = "Lost reason"
    _order = "name"

    name = fields.Char(required=True, translate=True)
    active = fields.Boolean(default=True)


class Tag(Model):
    _name = "crm.tag"
    _description = "CRM tag"
    _order = "name"
    _sql_constraints = [("name_uniq", "UNIQUE (name)", "Tag names must be unique")]

    name = fields.Char(required=True)
    color = fields.Integer(default=0)


class Source(Model):
    _name = "crm.source"
    _description = "Lead source"
    name = fields.Char(required=True)


class Medium(Model):
    _name = "crm.medium"
    _description = "Marketing medium"
    name = fields.Char(required=True)


class Campaign(Model):
    _name = "crm.campaign"
    _description = "Marketing campaign"
    name = fields.Char(required=True)
    company_id = fields.Many2one("core.company", ondelete="cascade")


class Team(Model):
    _name = "crm.team"
    _description = "Sales team"
    _order = "sequence, name"
    _inherit = ["chatter.thread"]
    _track_create = False

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    user_id = fields.Many2one("core.user", "Team leader", ondelete="set_null")
    member_ids = fields.One2many("crm.team_member", "team_id", "Members")
    member_user_ids = fields.Many2many(
        "core.user",
        relation="crm_team__member_user_ids",
        column1="team_id",
        column2="user_id",
        compute="_compute_member_users",
        store=True,
    )
    assignment_mode = fields.Selection(
        [("manual", "Manual"), ("round_robin", "Round robin"), ("balanced", "Balanced by weight")],
        default="round_robin",
        required=True,
    )
    assignment_max_per_day = fields.Integer(default=0, help="0 = no limit (per member)")
    alias_name = fields.Char("Email alias", help="Inbound address <alias>@<domain> creating leads for this team")
    alias_id = fields.Many2one("core.mail_alias", readonly=True, ondelete="set_null")
    webform_token = fields.Char(readonly=True, copy=False, index=True, default=lambda self: secrets.token_urlsafe(16))
    invoiced_target = fields.Monetary(currency_field="currency_id")
    currency_id = fields.Many2one(
        "core.currency",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).elevated().currency_id.ids[0],
    )
    color = fields.Integer(default=0)
    company_id = fields.Many2one("core.company", ondelete="cascade")
    rule_ids = fields.One2many("crm.assignment_rule", "team_id", "Assignment rules")
    active = fields.Boolean(default=True)

    @api.depends("member_ids.user_id", "member_ids.active")
    def _compute_member_users(self) -> None:
        for team in self:
            team.member_user_ids = [m.user_id.ids[0] for m in team.member_ids if m.active and m.user_id]

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        teams = super().create(vals_list)
        teams._sync_alias()
        return teams

    def write(self, vals):  # type: ignore[no-untyped-def]
        result = super().write(vals)
        if "alias_name" in vals:
            self._sync_alias()
        return result

    def _sync_alias(self) -> None:
        aliases = self.env["core.mail_alias"].elevated("team alias")
        for team in self:
            alias = team.elevated().alias_id
            if team.alias_name:
                vals = {
                    "alias_name": team.alias_name,
                    "model_name": "crm.lead",
                    "defaults": {"team_id": team.ids[0], "type": "lead"},
                }
                if alias:
                    alias.write(vals)
                else:
                    team.elevated().write({"alias_id": aliases.create(vals).ids[0]})
            elif alias:
                team.elevated().write({"alias_id": None})
                alias.unlink()

    @api.expose
    def action_regenerate_webform_token(self) -> bool:
        for team in self:
            team.write({"webform_token": secrets.token_urlsafe(16)})
        return True


class TeamMember(Model):
    _name = "crm.team_member"
    _description = "Sales team member"
    _order = "team_id, id"
    _rec_name = "user_id"
    _sql_constraints = [("team_user_uniq", "UNIQUE (team_id, user_id)", "A user is a member of a team only once")]

    team_id = fields.Many2one("crm.team", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one("core.user", required=True, ondelete="cascade")
    assignment_weight = fields.Integer(default=1, help="Relative share of new leads (balanced mode)")
    assignment_max = fields.Integer(default=0, help="Daily maximum (0 = team default)")
    assignment_domain = fields.Json(help="Only leads matching this domain are assigned to this member")
    assigned_count = fields.Integer(readonly=True, default=0, help="Leads assigned by the round robin")
    last_assigned_at = fields.Datetime(readonly=True)
    active = fields.Boolean(default=True)


class AssignmentRule(Model):
    _name = "crm.assignment_rule"
    _description = "Lead assignment rule"
    _order = "sequence, id"

    name = fields.Char(required=True)
    team_id = fields.Many2one(
        "crm.team", ondelete="cascade", help="Rules of a team apply to that team's unassigned leads; empty = all"
    )
    domain = fields.Json(required=True, default=list)
    assign_team_id = fields.Many2one("crm.team", "Route to team", ondelete="cascade")
    assign_user_id = fields.Many2one("core.user", "Assign to", ondelete="set_null")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    @api.constrains("domain", "assign_team_id", "assign_user_id")
    def _check(self) -> None:
        for rule in self:
            Domain(rule.domain or [])
            if not rule.assign_team_id and not rule.assign_user_id:
                raise ValidationError("A rule routes to a team or a salesperson", field="assign_team_id")


class ScoringRule(Model):
    _name = "crm.scoring_rule"
    _description = "Lead scoring rule"
    _order = "sequence, id"

    name = fields.Char(required=True)
    domain = fields.Json(required=True, default=list, help="Leads matching this domain get the points")
    points = fields.Integer(required=True, default=10)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one("core.company", ondelete="cascade")
    active = fields.Boolean(default=True)

    @api.constrains("domain")
    def _check_domain(self) -> None:
        for rule in self:
            try:
                Domain(rule.domain or [])
            except Exception as exc:
                raise ValidationError(f"Invalid domain: {exc}", field="domain") from exc

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        rules = super().create(vals_list)
        rules._schedule_rescore()
        return rules

    def write(self, vals):  # type: ignore[no-untyped-def]
        result = super().write(vals)
        if set(vals) & {"domain", "points", "active", "company_id"}:
            self._schedule_rescore()
        return result

    def unlink(self) -> bool:
        result = super().unlink()
        self._schedule_rescore()
        return result

    def _schedule_rescore(self) -> None:
        """1.4-h: a rule change rescores the open leads in a background job."""
        from tessera.jobs import enqueue

        enqueue(self.env.elevated(), "crm.rescore", {}, description="Recompute lead scores", queue="default")

    @api.model
    def preview(self, domain: Any) -> int:
        return self.env["crm.lead"].search_count([("active", "=", True), Domain(domain).node])
