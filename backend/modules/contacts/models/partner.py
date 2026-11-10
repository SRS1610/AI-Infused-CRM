"""Contacts: companies, people and their addresses."""

from __future__ import annotations

import re
from typing import Any

from tessera.exceptions import ValidationError
from tessera.orm import Model, api, fields

PARTNER_TYPES = [
    ("contact", "Contact"),
    ("invoice", "Invoice address"),
    ("delivery", "Delivery address"),
    ("other", "Other address"),
]
ADDRESS_FIELDS = ("street", "street2", "zip", "city", "state_id", "country_id")


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    match = re.search(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+", value)
    return match.group(0).lower() if match else value.strip().lower() or None


def normalize_phone(value: str | None) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    return digits[-9:] if len(digits) >= 6 else None


class Tag(Model):
    _name = "contacts.tag"
    _description = "Contact tag"
    _order = "name"

    name = fields.Char(required=True, translate=True)
    color = fields.Integer(default=0)
    parent_id = fields.Many2one("contacts.tag", ondelete="cascade")
    active = fields.Boolean(default=True)


class Industry(Model):
    _name = "contacts.industry"
    _description = "Industry"
    _order = "name"

    name = fields.Char(required=True, translate=True)
    full_name = fields.Char()
    active = fields.Boolean(default=True)


class Title(Model):
    _name = "contacts.title"
    _description = "Title"
    _order = "name"

    name = fields.Char(required=True, translate=True)
    shortcut = fields.Char(translate=True)


class Partner(Model):
    _name = "contacts.partner"
    _description = "Contact"
    _order = "display_name, id"
    _inherit = ["chatter.thread"]
    _audit = True
    _global_search = ["display_name", "email", "phone", "mobile", "ref", "vat", "city"]
    _track_create = True

    name = fields.Char(index="trigram", tracking=True)
    display_name = fields.Char(compute="_compute_display_name", store=True, index="trigram")
    is_company = fields.Boolean("Is a company", default=False, tracking=True)
    parent_id = fields.Many2one("contacts.partner", "Company", ondelete="restrict", tracking=True)
    child_ids = fields.One2many("contacts.partner", "parent_id", "Contacts & addresses")
    commercial_partner_id = fields.Many2one(
        "contacts.partner", "Commercial entity", compute="_compute_commercial_partner", store=True, ondelete="set_null"
    )
    type = fields.Selection(PARTNER_TYPES, default="contact", required=True)
    email = fields.Char(tracking=True, pii=True)
    email_normalized = fields.Char(compute="_compute_email_normalized", store=True, index=True)
    phone = fields.Char(pii=True)
    mobile = fields.Char(pii=True)
    phone_normalized = fields.Char(compute="_compute_phone_normalized", store=True, index=True)
    website = fields.Char()
    description = fields.Text("About", help="Public description of the company, e.g. from its website")
    social_links = fields.Text(help="Public profiles (LinkedIn, X, Facebook …), one link per line")
    logo_url = fields.Char("Logo URL", help="Address of the company's logo, e.g. on its website")
    vat = fields.Char("Tax ID", index=True)
    ref = fields.Char("Reference", index=True)
    lang = fields.Char(default=lambda self: self.env.registry.default_lang)
    tz = fields.Char("Time zone")
    user_id = fields.Many2one("core.user", "Salesperson", ondelete="set_null", tracking=True)
    industry_id = fields.Many2one("contacts.industry", ondelete="set_null")
    title_id = fields.Many2one("contacts.title", ondelete="set_null")
    function = fields.Char("Job position")
    street = fields.Char()
    street2 = fields.Char()
    city = fields.Char()
    zip = fields.Char()
    state_id = fields.Many2one("core.country_state", "State", ondelete="set_null")
    country_id = fields.Many2one("core.country", "Country", ondelete="set_null")
    latitude = fields.Float(digits=(10, 7))
    longitude = fields.Float(digits=(10, 7))
    comment = fields.Html("Notes")
    image = fields.Image(attachment=True, max_size=(512, 512))
    tag_ids = fields.Many2many(
        "contacts.tag", relation="contacts_partner__tag_ids", column1="partner_id", column2="tag_id"
    )
    company_id = fields.Many2one("core.company", ondelete="set_null", help="Empty = shared by every company")
    color = fields.Integer(default=0)
    active = fields.Boolean(default=True)

    @api.depends("name", "parent_id.name", "type", "is_company")
    def _compute_display_name(self) -> None:
        labels = dict(PARTNER_TYPES)
        for p in self:
            name = p.name or ("" if p.type == "contact" else labels.get(p.type, ""))
            if p.parent_id and not p.is_company:
                name = f"{p.parent_id.name or ''}, {name}" if name else p.parent_id.name or ""
            p.display_name = name or None

    @api.depends("is_company", "parent_id.commercial_partner_id")
    def _compute_commercial_partner(self) -> None:
        for p in self:
            if p.is_company or not p.parent_id:
                p.commercial_partner_id = p.ids[0] if p.ids else None
            else:
                parent = p.parent_id.commercial_partner_id or p.parent_id
                p.commercial_partner_id = parent.ids[0] if parent else None

    @api.depends("email")
    def _compute_email_normalized(self) -> None:
        for p in self:
            p.email_normalized = normalize_email(p.email)

    @api.depends("phone", "mobile")
    def _compute_phone_normalized(self) -> None:
        for p in self:
            p.phone_normalized = normalize_phone(p.phone) or normalize_phone(p.mobile)

    @api.constrains("name", "type", "is_company", "parent_id")
    def _check_name(self) -> None:
        for p in self:
            if not p.name and (p.type == "contact" or p.is_company):
                raise ValidationError("A contact needs a name", field="name")
            if p.parent_id and p.parent_id.ids == p.ids:
                raise ValidationError("A contact cannot be its own company", field="parent_id")
            ancestor, seen = p.parent_id, set()
            while ancestor:
                if ancestor.ids[0] in seen or (p.ids and ancestor.ids[0] == p.ids[0]):
                    raise ValidationError("Recursive company hierarchy", field="parent_id")
                seen.add(ancestor.ids[0])
                ancestor = ancestor.parent_id

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:  # addresses inherit the company address when none is given
            if (
                vals.get("parent_id")
                and vals.get("type", "contact") == "contact"
                and not any(vals.get(f) for f in ADDRESS_FIELDS)
            ):
                parent = self.browse([vals["parent_id"]]).elevated()
                for f in ADDRESS_FIELDS:
                    value = parent[f]
                    vals[f] = value.ids[0] if hasattr(value, "_ids") and value else (value or None)
        return super().create(batch)

    def _ancestor_ids(self) -> set[int]:
        out: set[int] = set()
        parent = self.parent_id
        while parent and parent.ids[0] not in out:
            out.add(parent.ids[0])
            parent = parent.parent_id
        return out

    def write(self, vals):  # type: ignore[no-untyped-def]
        parent_id = vals.get("parent_id")
        if parent_id:  # refuse cycles before anything depending on the hierarchy is recomputed
            ancestors = {int(parent_id)} | self.browse([int(parent_id)]).elevated()._ancestor_ids()
            if set(self.ids) & ancestors:
                raise ValidationError("Recursive company hierarchy", field="parent_id")
        return super().write(vals)

    # -- duplicates ---------------------------------------------------------------------------------

    @api.expose
    def find_duplicates(self, limit: int = 20) -> list[dict[str, Any]]:
        """Rule-based candidates: same email, same phone (last 9 digits), or same name in the same company."""
        self.ensure_one()
        me = self.elevated()
        found: dict[int, set[str]] = {}

        def add(records: Model, reason: str) -> None:
            for r in records:
                if r.ids[0] != self.ids[0]:
                    found.setdefault(r.ids[0], set()).add(reason)

        partners = self.env["contacts.partner"]
        if me.email_normalized:
            add(partners.search([("email_normalized", "=", me.email_normalized)], limit=limit), "email")
        if me.phone_normalized:
            add(partners.search([("phone_normalized", "=", me.phone_normalized)], limit=limit), "phone")
        if me.name:
            domain: list[Any] = [("name", "ilike", me.name)]
            same = partners.search(domain, limit=limit * 3).filtered(
                lambda r: (r.name or "").strip().lower() == me.name.strip().lower() and r.parent_id == me.parent_id
            )
            add(same, "name")
        found_records = partners.browse(list(found))
        return [
            {
                "id": r.ids[0],
                "display_name": r.display_name,
                "email": r.email,
                "phone": r.phone,
                "reasons": sorted(found[r.ids[0]]),
            }
            for r in found_records
        ][:limit]

    @api.expose
    def action_merge(
        self, survivor_id: int | None = None, field_choices: dict[str, int] | None = None
    ) -> dict[str, Any]:
        """Merge ``self`` (2+ contacts) into ``survivor_id`` (default: the oldest)."""
        from modules.contacts.merge import merge_partners

        survivor = self.browse([survivor_id]) if survivor_id else self.sorted(lambda r: r.ids[0])[:1]
        others = self - survivor
        log = merge_partners(self.env, survivor, others, field_choices or {})
        return {"merge_id": log.ids[0], "survivor_id": survivor.ids[0]}


class User(Model):
    """Every user is also a contact (chatter authorship, followers and portal access share one identity)."""

    _extend = "core.user"

    partner_id = fields.Many2one("contacts.partner", "Contact", ondelete="restrict", index=True)

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        partners = self.env["contacts.partner"].elevated("user contact").with_context(tracking_disable=True)
        for vals in batch:
            if not vals.get("partner_id"):
                partner = partners.create({"name": vals.get("name") or vals.get("login"), "email": vals.get("email")})
                vals["partner_id"] = partner.ids[0]
        return super().create(batch)

    def write(self, vals):  # type: ignore[no-untyped-def]
        result = super().write(vals)
        sync = {k: vals[k] for k in ("name", "email") if k in vals}
        if sync:
            for user in self.elevated():
                if user.partner_id:
                    user.partner_id.with_context(tracking_disable=True).write(sync)
        return result
