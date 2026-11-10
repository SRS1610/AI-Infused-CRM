"""Leads and opportunities."""

from __future__ import annotations

from typing import Any

from modules.contacts.models.partner import normalize_email
from tessera.exceptions import UserError
from tessera.orm import Model, api, fields

PRIORITIES = [("0", "Normal"), ("1", "Good"), ("2", "Very good"), ("3", "Excellent")]


def _default_stage(model: Model) -> int | None:
    stage = model.env["crm.stage"].elevated().search([("is_won", "=", False)], order="sequence, id", limit=1)
    return stage.ids[0] if stage else None


class Lead(Model):
    _name = "crm.lead"
    _description = "Lead / opportunity"
    _order = "priority desc, id desc"
    _inherit = ["chatter.thread"]
    _audit = True
    _global_search = ["name", "partner_name", "contact_name", "email_from", "partner_id"]
    _indexes = [("stage_open", ["stage_id", "active"]), ("team_user", ["team_id", "user_id"])]

    name = fields.Char("Opportunity", required=True, index="trigram", tracking=True)
    type = fields.Selection(
        [("lead", "Lead"), ("opportunity", "Opportunity")], default="opportunity", required=True, index=True
    )
    partner_id = fields.Many2one("contacts.partner", "Customer", ondelete="set_null", tracking=True)
    partner_name = fields.Char("Company name")
    contact_name = fields.Char("Contact name")
    email_from = fields.Char("Email", pii=True)
    email_normalized = fields.Char(compute="_compute_email_normalized", store=True, index=True)
    phone = fields.Char(pii=True)
    mobile = fields.Char(pii=True)
    website = fields.Char()
    function = fields.Char("Job position")
    street = fields.Char()
    city = fields.Char()
    zip = fields.Char()
    country_id = fields.Many2one("core.country", ondelete="set_null")
    team_id = fields.Many2one("crm.team", "Sales team", ondelete="set_null", tracking=True)
    user_id = fields.Many2one("core.user", "Salesperson", ondelete="set_null", tracking=True)
    stage_id = fields.Many2one(
        "crm.stage", "Stage", ondelete="restrict", default=_default_stage, tracking=True, index=True
    )
    is_won = fields.Boolean(related="stage_id.is_won", store=True)
    probability = fields.Float(default=10.0, tracking=True)
    expected_revenue = fields.Monetary("Expected revenue", currency_field="currency_id", tracking=True)
    prorated_revenue = fields.Monetary(
        "Weighted revenue", currency_field="currency_id", compute="_compute_prorated", store=True
    )
    currency_id = fields.Many2one(
        "core.currency",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).elevated().currency_id.ids[0],
        ondelete="restrict",
    )
    date_deadline = fields.Date("Expected closing", tracking=True, index=True)
    date_closed = fields.Datetime(readonly=True, copy=False)
    date_conversion = fields.Datetime(readonly=True, copy=False)
    date_last_stage_update = fields.Datetime(readonly=True, copy=False)
    lost = fields.Boolean(readonly=True, copy=False, index=True)
    lost_reason_id = fields.Many2one("crm.lost_reason", ondelete="set_null", tracking=True)
    lost_feedback = fields.Text()
    priority = fields.Selection(PRIORITIES, default="0", index=True)
    tag_ids = fields.Many2many("crm.tag", relation="crm_lead__tag_ids", column1="lead_id", column2="tag_id")
    source_id = fields.Many2one("crm.source", ondelete="set_null")
    medium_id = fields.Many2one("crm.medium", ondelete="set_null")
    campaign_id = fields.Many2one("crm.campaign", ondelete="set_null")
    description = fields.Html("Notes")
    score = fields.Integer(readonly=True, index=True, help="Sum of the points of matching scoring rules")
    score_factors = fields.Json(readonly=True, help="[{rule, points}] — why the lead has this score")
    color = fields.Integer(default=0)
    company_id = fields.Many2one(
        "core.company",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).ids[0],
        ondelete="restrict",
        index=True,
    )
    quotation_ids = fields.One2many("sale.quotation", "opportunity_id", "Quotations")
    quotation_count = fields.Integer(compute="_compute_quotation_count")
    intake_ids = fields.One2many("crm.lead_intake", "lead_id", "Intake log")
    active = fields.Boolean(default=True)

    @api.depends("email_from")
    def _compute_email_normalized(self) -> None:
        for lead in self:
            lead.email_normalized = normalize_email(lead.email_from)

    @api.depends("expected_revenue", "probability")
    def _compute_prorated(self) -> None:
        for lead in self:
            lead.prorated_revenue = (lead.expected_revenue or 0) * (lead.probability or 0) / 100

    def _compute_quotation_count(self) -> None:
        counts: dict[int, int] = {}
        if self.ids:
            rows = self.env.cr.fetchall(
                "SELECT opportunity_id, COUNT(*) FROM sale_quotation"
                " WHERE opportunity_id = ANY(:ids) GROUP BY opportunity_id",
                {"ids": list(self.ids)},
            )
            counts = {r[0]: r[1] for r in rows}
        for lead in self:
            lead.quotation_count = counts.get(lead.ids[0], 0) if lead.ids else 0

    # -- life cycle ------------------------------------------------------------------------------------

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        from modules.crm.services import apply_assignment, compute_scores

        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        stages = self.env["crm.stage"].elevated()
        for vals in batch:
            if vals.get("stage_id") and "probability" not in vals:
                vals["probability"] = stages.browse([vals["stage_id"]]).probability
            if vals.get("partner_id"):
                self._fill_from_partner(vals)
            vals.setdefault("date_last_stage_update", self.env.now())
        leads = super().create(batch)
        to_assign = leads.filtered(lambda lead: not lead.user_id)
        if to_assign and not self.env.context.get("crm_no_assign"):
            apply_assignment(to_assign)
        compute_scores(leads)
        return leads

    def write(self, vals):  # type: ignore[no-untyped-def]
        from modules.crm.services import compute_scores

        vals = dict(vals)
        if vals.get("stage_id"):
            stage = self.env["crm.stage"].elevated().browse([vals["stage_id"]])
            vals.setdefault("date_last_stage_update", self.env.now())
            if "probability" not in vals:
                vals["probability"] = 100.0 if stage.is_won else stage.probability
            if stage.is_won and "date_closed" not in vals:
                vals["date_closed"] = self.env.now()
        if vals.get("partner_id"):
            self._fill_from_partner(vals, only_blank=self)
        result = super().write(vals)
        if not self.env.context.get("crm_scoring") and set(vals) - {"score", "score_factors"}:
            compute_scores(self)
        return result

    def _fill_from_partner(self, vals: dict[str, Any], only_blank: Model | None = None) -> None:
        partner = self.env["contacts.partner"].elevated().browse([vals["partner_id"]])
        mapping = {
            "email_from": "email",
            "phone": "phone",
            "mobile": "mobile",
            "website": "website",
            "street": "street",
            "city": "city",
            "zip": "zip",
            "country_id": "country_id",
            "function": "function",
        }
        for lead_field, partner_field in mapping.items():
            if lead_field in vals:
                continue
            if only_blank is not None and any(lead[lead_field] for lead in only_blank):
                continue
            value = partner[partner_field]
            if value:
                vals[lead_field] = value.ids[0] if hasattr(value, "_ids") else value
        if not vals.get("partner_name") and partner.commercial_partner_id and partner.commercial_partner_id.is_company:
            vals.setdefault("partner_name", partner.commercial_partner_id.name)

    # -- inbound email -----------------------------------------------------------------------------------

    @api.model
    def message_new(self, msg: dict[str, Any], defaults: dict[str, Any] | None = None) -> Model:
        """1.4-f: email to the team alias → lead with the sender as contact and the body as description."""
        partners = self.env["contacts.partner"].elevated("lead intake")
        email = normalize_email(msg.get("from"))
        partner = partners.search([("email_normalized", "=", email)], limit=1) if email else partners.browse([])
        if not partner and email:
            partner = partners.create({"name": msg.get("from_name") or email, "email": email})
        vals = dict(defaults or {})
        vals.update(
            {
                "name": msg.get("subject") or f"Email from {msg.get('from_name') or email}",
                "email_from": email,
                "contact_name": msg.get("from_name") or None,
                "description": msg.get("body") or "",
                "partner_id": partner.ids[0] if partner else None,
            }
        )
        vals.setdefault("type", "lead")
        lead = self.create(vals)
        self.env["crm.lead_intake"].elevated().create(
            {
                "lead_id": lead.ids[0],
                "channel": "email",
                "source_ref": msg.get("message_id"),
                "payload": {"from": email, "subject": msg.get("subject")},
            }
        )
        return lead

    # -- outcome -------------------------------------------------------------------------------------------

    def _won_stage(self) -> Model:
        stages = self.env["crm.stage"].elevated().search([("is_won", "=", True)], order="sequence, id")
        team = self[:1].team_id
        for stage in stages:
            if not stage.team_ids or (team and team.ids[0] in stage.team_ids.ids):
                return stage
        if not stages:
            raise UserError("Define a won stage first")
        return stages[:1]

    @api.expose
    def action_set_won(self) -> bool:
        for lead in self:
            if lead.lost or not lead.active:
                raise UserError("Restore the opportunity before marking it won")
            lead.write(
                {
                    "stage_id": lead._won_stage().ids[0],
                    "probability": 100.0,
                    "date_closed": self.env.now(),
                    "type": "opportunity",
                }
            )
        return True

    @api.expose
    def action_set_lost(self, lost_reason_id: int | None = None, feedback: str | None = None) -> bool:
        self.write(
            {
                "lost": True,
                "active": False,
                "probability": 0.0,
                "lost_reason_id": lost_reason_id,
                "lost_feedback": feedback,
                "date_closed": self.env.now(),
            }
        )
        return True

    @api.expose
    def action_restore(self) -> bool:
        stage = self.env["crm.stage"].elevated().search([("is_won", "=", False)], order="sequence, id", limit=1)
        for lead in self.with_context(active_test=False):
            vals: dict[str, Any] = {"lost": False, "active": True, "lost_reason_id": None, "date_closed": None}
            if lead.is_won and stage:
                vals["stage_id"] = stage.ids[0]
            lead.write(vals)
        return True

    @api.expose
    def action_convert(
        self,
        partner_mode: str = "create",
        partner_id: int | None = None,
        merge_into_id: int | None = None,
        user_id: int | None = None,
        team_id: int | None = None,
    ) -> dict[str, Any]:
        """Lead → opportunity. ``partner_mode``: create | existing | none. With ``merge_into_id`` the lead is
        merged into that opportunity (chatter, activities, attachments move; the lead is archived)."""
        from modules.crm.services import convert_lead, merge_leads

        self.ensure_one()
        partner = convert_partner(self, partner_mode, partner_id)
        if merge_into_id:
            target = self.browse([merge_into_id])
            merge_leads(target, self)
            if partner and not target.partner_id:
                target.write({"partner_id": partner.ids[0]})
            return {"opportunity_id": target.ids[0], "merged": True}
        convert_lead(self, partner, user_id, team_id)
        return {"opportunity_id": self.ids[0], "merged": False}

    @api.expose
    def action_new_quotation(self) -> dict[str, Any]:
        self.ensure_one()
        if not self.partner_id:
            raise UserError("Set the customer before creating a quotation")
        quote = self.env["sale.quotation"].create(
            {
                "partner_id": self.partner_id.ids[0],
                "opportunity_id": self.ids[0],
                "user_id": (self.user_id or self.env.user).ids[0],
            }
        )
        return {"quotation_id": quote.ids[0], "action": "sale.action_quotations"}

    @api.expose
    def action_find_matches(self) -> list[dict[str, Any]]:
        """Open opportunities of the same customer or email (merge candidates)."""
        self.ensure_one()
        domain: list[Any] = [("id", "!=", self.ids[0]), ("type", "=", "opportunity")]
        alternatives: list[Any] = []
        if self.partner_id:
            alternatives.append(["partner_id", "=", self.partner_id.ids[0]])
        if self.email_normalized:
            alternatives.append(["email_normalized", "=", self.email_normalized])
        if not alternatives:
            return []
        found = self.search([*domain, {"any": alternatives}], limit=10)
        return [
            {"id": r.ids[0], "name": r.name, "stage": r.stage_id.name, "expected_revenue": r.expected_revenue}
            for r in found
        ]


def convert_partner(lead: Model, mode: str, partner_id: int | None) -> Model:
    partners = lead.env["contacts.partner"]
    if mode == "existing":
        if not partner_id:
            raise UserError("Choose the existing customer")
        return partners.browse([partner_id])
    if mode == "none" or lead.partner_id:
        return lead.partner_id
    company = None
    if lead.partner_name:
        company = partners.search(
            [("is_company", "=", True), ("name", "=", lead.partner_name)], limit=1
        ) or partners.create(
            {
                "name": lead.partner_name,
                "is_company": True,
                "website": lead.website,
                "city": lead.city,
                "zip": lead.zip,
                "street": lead.street,
                "country_id": lead.country_id.ids[0] if lead.country_id else None,
            }
        )
    name = lead.contact_name or (lead.email_from or "").split("@")[0] or lead.partner_name
    if not name:
        return company or partners.browse([])
    return partners.create(
        {
            "name": name,
            "parent_id": company.ids[0] if company else None,
            "email": lead.email_from,
            "phone": lead.phone,
            "mobile": lead.mobile,
            "function": lead.function,
        }
    )


class LeadIntake(Model):
    _name = "crm.lead_intake"
    _description = "Lead intake"
    _order = "id desc"
    _rec_name = "channel"
    _studio = False

    lead_id = fields.Many2one("crm.lead", required=True, ondelete="cascade", index=True)
    channel = fields.Selection(
        [("email", "Email"), ("webform", "Web form"), ("api", "API"), ("manual", "Manual"), ("import", "Import")],
        required=True,
    )
    source_ref = fields.Char(help="Message-ID, page URL, API client …")
    payload = fields.Json()


class Quotation(Model):
    _extend = "sale.quotation"

    opportunity_id = fields.Many2one("crm.lead", "Opportunity", ondelete="set_null", index=True, tracking=True)
