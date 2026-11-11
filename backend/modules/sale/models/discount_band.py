"""Approved discount bands and pricing-policy flags (sub-phase 2.6 AI features).

``sale.discount_band`` says how much discount a salesperson may give without asking: per product category
(and its sub-categories), customer segment (contact tags) and minimum quantity, with the minimum margin a price
must keep and the group that approves more. The discount suggestions (``sale.discount_suggest``) never leave
the band of a line; the pricing check (``sale.pricing_breach``) flags lines that do and records the decision of
the approver on ``sale.pricing_flag``.
"""

from __future__ import annotations

from typing import Any

from tessera.exceptions import AccessError, UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

FLAG_CODES = [
    ("below_margin", "Below the minimum margin"),
    ("below_pricelist", "Price below the pricelist beyond the band"),
    ("discount_above_band", "Discount above the approved band"),
    ("unusual_for_customer", "Unusual for this customer"),
]
FLAG_STATES = [("open", "To review"), ("approved", "Approved"), ("resolved", "Resolved")]


class DiscountBand(Model):
    _name = "sale.discount_band"
    _description = "Approved discount band"
    _order = "sequence, id"
    _audit = True

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10, help="Among equally specific bands, the first one applies")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        index=True,
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).ids[0],
    )
    category_id = fields.Many2one(
        "product.category",
        "Product category",
        ondelete="cascade",
        help="Applies to products of this category and its sub-categories (empty = every product)",
    )
    partner_tag_ids = fields.Many2many(
        "contacts.tag",
        "Customer segments",
        relation="sale_discount_band_tag_rel",
        column1="band_id",
        column2="tag_id",
        help="Applies to customers with one of these tags (empty = every customer)",
    )
    min_quantity = fields.Float("From quantity", default=0.0, help="Applies to lines of at least this quantity")
    min_discount = fields.Float("Minimum discount %", default=0.0)
    max_discount = fields.Float("Maximum discount %", required=True, default=0.0)
    min_margin = fields.Float(
        "Minimum margin %",
        default=0.0,
        help="A net unit price below cost plus this margin is flagged by the pricing check",
    )
    approver_group_id = fields.Many2one(
        "core.group",
        "Approved above the band by",
        ondelete="set_null",
        default=lambda self: self.env.ref("sale.group_sale_manager", raise_if_not_found=False),
        help="Members of this group approve discounts above the band (default: sales administrators)",
    )
    note = fields.Text()

    @api.constrains("min_discount", "max_discount", "min_margin", "min_quantity")
    def _check_band(self) -> None:
        for band in self:
            low, high = band.min_discount or 0.0, band.max_discount or 0.0
            if not (0 <= low <= high <= 100):
                raise ValidationError(
                    _("A discount band goes from a minimum to a maximum between 0 and 100 %"), field="max_discount"
                )
            if not (0 <= (band.min_margin or 0.0) <= 1000):
                raise ValidationError(_("The minimum margin is between 0 and 1000 %"), field="min_margin")
            if (band.min_quantity or 0.0) < 0:
                raise ValidationError(_("The quantity of a band cannot be negative"), field="min_quantity")

    def _matches(self, category: Model | None, tag_ids: set[int], quantity: float) -> int | None:
        """Specificity of this band for a line (higher is more specific), or None when it does not apply."""
        self.ensure_one()
        rank = 0
        if self.category_id:
            ancestors = category._ancestor_ids() if category else []
            if self.category_id.ids[0] not in ancestors:
                return None
            rank += 100 * (len(ancestors) - ancestors.index(self.category_id.ids[0]))
        if self.partner_tag_ids:
            if not set(self.partner_tag_ids.ids) & tag_ids:
                return None
            rank += 50
        if (self.min_quantity or 0.0) > float(quantity or 0.0) + 1e-9:
            return None
        return rank

    def _for_line(self, company: Model, product: Model, partner: Model | None, quantity: float) -> Model:
        """The most specific active band of ``company`` for ``product`` sold to ``partner`` (or an empty set).

        Ties on specificity go to the larger minimum quantity, then the band's sequence."""
        bands = self.elevated("discount band lookup").search([("company_id", "=", company.ids[0])])
        product = product.elevated()
        category = product.category_id or None
        tags: set[int] = set()
        if partner:
            p = partner.elevated()
            tags = set(p.tag_ids.ids) | set(p.commercial_partner_id.tag_ids.ids if p.commercial_partner_id else [])
        best: tuple[Any, ...] | None = None
        chosen = None
        for band in bands:
            rank = band._matches(category, tags, quantity)
            if rank is None:
                continue
            key = (rank, band.min_quantity or 0.0, -(band.sequence or 0), -band.ids[0])
            if best is None or key > best:
                best, chosen = key, band
        return self.browse(chosen.ids if chosen else [])


class PricingFlag(Model):
    _name = "sale.pricing_flag"
    _description = "Pricing-policy flag"
    _order = "quotation_id, id"
    _rec_name = "message"

    quotation_id = fields.Many2one("sale.quotation", "Quotation", required=True, ondelete="cascade", index=True)
    line_id = fields.Many2one("sale.quotation_line", "Line", ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="quotation_id.company_id", store=True, index=True)
    code = fields.Selection(FLAG_CODES, required=True, index=True)
    severity = fields.Selection([("warning", "Warning"), ("breach", "Breach")], required=True, default="breach")
    message = fields.Char(required=True)
    band_id = fields.Many2one("sale.discount_band", "Band", ondelete="set_null")
    value = fields.Float("Discount %", help="Discount (or effective discount) of the line when it was flagged")
    signature = fields.Char(index=True, help="Code, line and rounded values: a changed line is a new flag")
    state = fields.Selection(FLAG_STATES, default="open", required=True, index=True, tracking=True)
    approved_by = fields.Many2one("core.user", readonly=True)
    approved_at = fields.Datetime(readonly=True)
    ai_run_id = fields.Integer("AI run", help="The audit run of the check that raised the flag")

    @api.expose
    def action_approve(self) -> bool:
        """Approve the price (members of the band's approver group, else sales administrators)."""
        mine = set(self.env.user.all_group_ids()) if self.env.uid > 0 else set()
        manager = self.env.registry.security.has_groups(self.env, ["sale.group_sale_manager"])
        for flag in self:
            flag.check_access("read")
            rec = flag.elevated()
            if rec.state != "open":
                raise UserError(_("This flag is already {state}", state=dict(FLAG_STATES)[rec.state]))
            group = rec.band_id.approver_group_id if rec.band_id else None
            if not manager and not (group and group.ids[0] in mine):
                raise AccessError(_("Only the approvers of this discount band can approve it"))
            rec.write({"state": "approved", "approved_by": self.env.uid, "approved_at": self.env.now()})
        return True
