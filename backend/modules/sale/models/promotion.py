"""Promotions: automatic or code programs with conditions (rules) and rewards. The engine that turns them into
reward lines on an order is ``modules.sale.promotions``."""

from __future__ import annotations

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

REWARD_TYPES = [
    ("discount_order", "Percentage on the order"),
    ("discount_cheapest", "Percentage on the cheapest line"),
    ("fixed_amount", "Fixed amount"),
    ("free_product", "Free product"),
]


class Promotion(Model):
    _name = "sale.promotion"
    _description = "Promotion"
    _order = "sequence, id"
    _audit = True
    _sql_constraints = [("code_company_uniq", "UNIQUE (code, company_id)", "A promotion code must be unique")]

    name = fields.Char(required=True, translate=True, help="Shown on the reward lines")
    sequence = fields.Integer(default=10, help="Promotions apply in this order")
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "core.company",
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
        help="Empty = every company",
    )
    currency_id = fields.Many2one(
        "core.currency",
        "Currency",
        required=True,
        ondelete="restrict",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).elevated().currency_id.ids[0],
        help="Currency of the minimum amounts and fixed rewards (converted to the order's currency)",
    )
    program_type = fields.Selection(
        [("automatic", "Automatic"), ("code", "With a code")],
        "Applies",
        required=True,
        default="automatic",
        help="Automatic promotions apply to every eligible order; code promotions once the code is entered",
    )
    code = fields.Char(help="The code customers give (case-insensitive)")
    date_from = fields.Date("Valid from")
    date_to = fields.Date("Valid until")
    max_usage = fields.Integer(
        "Maximum uses", default=0, help="Number of sales orders that can use the promotion (0 = unlimited)"
    )
    usage_count = fields.Integer("Uses", compute="_compute_usage_count", help="Sales orders that used it")
    rule_ids = fields.One2many(
        "sale.promotion_rule",
        "promotion_id",
        "Conditions",
        copy=True,
        help="The order is eligible when one condition is met (no condition: every order)",
    )
    reward_ids = fields.One2many("sale.promotion_reward", "promotion_id", "Rewards", copy=True)
    note = fields.Text("Internal notes")

    def _compute_usage_count(self) -> None:
        for promo in self:
            promo.usage_count = promo._usage(exclude_order_id=None)

    def _usage(self, exclude_order_id: int | None) -> int:
        """Sales orders (confirmed) with reward lines of this promotion."""
        self.ensure_one()
        domain: list[object] = [
            ("promotion_id", "=", self.ids[0]),
            ("is_reward_line", "=", True),
            ("quotation_id.state", "=", "accepted"),
        ]
        if exclude_order_id:
            domain.append(("quotation_id", "!=", exclude_order_id))
        lines = self.env["sale.quotation_line"].elevated().search(domain)
        return len(set(lines.mapped("quotation_id").ids))

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("code"):
                vals["code"] = str(vals["code"]).strip().upper()
        return super().create(batch)

    def write(self, vals):  # type: ignore[no-untyped-def]
        if vals.get("code"):
            vals = {**vals, "code": str(vals["code"]).strip().upper()}
        return super().write(vals)

    @api.constrains("program_type", "code", "date_from", "date_to", "max_usage")
    def _check_promotion(self) -> None:
        for promo in self:
            if promo.program_type == "code" and not (promo.code or "").strip():
                raise ValidationError(_("A code promotion needs a code"), field="code")
            if promo.date_from and promo.date_to and promo.date_to < promo.date_from:
                raise ValidationError(_("The promotion ends before it starts"), field="date_to")
            if (promo.max_usage or 0) < 0:
                raise ValidationError(_("The maximum number of uses cannot be negative"), field="max_usage")


class PromotionRule(Model):
    _name = "sale.promotion_rule"
    _description = "Promotion condition"
    _order = "promotion_id, id"

    promotion_id = fields.Many2one("sale.promotion", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="promotion_id.company_id", store=True)
    minimum_amount = fields.Float(
        "Minimum untaxed amount", digits=(16, 2), default=0.0, help="Untaxed amount of the order (before rewards)"
    )
    minimum_qty = fields.Float(
        "Minimum quantity", digits=(16, 3), default=0.0, help="Quantity of the products below (all products if none)"
    )
    product_ids = fields.Many2many(
        "product.product",
        "Products",
        relation="sale_promotion_rule_product_rel",
        column1="rule_id",
        column2="product_id",
    )
    category_ids = fields.Many2many(
        "product.category",
        "Product categories",
        relation="sale_promotion_rule_category_rel",
        column1="rule_id",
        column2="category_id",
        help="Products of these categories (and their sub-categories) count",
    )

    @api.constrains("minimum_amount", "minimum_qty")
    def _check_minimums(self) -> None:
        for rule in self:
            if (rule.minimum_amount or 0) < 0 or (rule.minimum_qty or 0) < 0:
                raise ValidationError(_("Minimums cannot be negative"), field="minimum_amount")

    def _matches_product(self, product: Model) -> bool:
        """Whether ``product`` counts for this condition (every product when none is listed)."""
        if not self.product_ids and not self.category_ids:
            return True
        if product.ids[0] in self.product_ids.ids or (
            product.template_id and product.template_id.ids[0] in self.product_ids.ids
        ):
            return True
        if product.category_id and self.category_ids:
            return bool(set(product.category_id._ancestor_ids()) & set(self.category_ids.ids))
        return False


class PromotionReward(Model):
    _name = "sale.promotion_reward"
    _description = "Promotion reward"
    _order = "promotion_id, id"

    promotion_id = fields.Many2one("sale.promotion", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="promotion_id.company_id", store=True)
    reward_type = fields.Selection(REWARD_TYPES, "Reward", required=True, default="discount_order")
    discount_percent = fields.Float("Discount %", digits=(5, 2), default=10.0)
    fixed_amount = fields.Float("Amount", digits=(16, 2), default=0.0, help="In the promotion's currency")
    free_product_id = fields.Many2one(
        "product.product", "Free product", ondelete="restrict", domain=[["is_sellable", "=", True]]
    )
    free_quantity = fields.Float("Free quantity", digits=(16, 3), default=1.0)
    description = fields.Char(translate=True, help="Label of the reward line (default: the promotion's name)")

    @api.constrains("reward_type", "discount_percent", "fixed_amount", "free_product_id", "free_quantity")
    def _check_reward(self) -> None:
        for reward in self:
            kind = reward.reward_type
            if kind in ("discount_order", "discount_cheapest") and not 0 < (reward.discount_percent or 0) <= 100:
                raise ValidationError(_("A discount is between 0 and 100 %"), field="discount_percent")
            if kind == "fixed_amount" and (reward.fixed_amount or 0) <= 0:
                raise ValidationError(_("A fixed reward needs a positive amount"), field="fixed_amount")
            if kind == "free_product" and (not reward.free_product_id or (reward.free_quantity or 0) <= 0):
                raise ValidationError(_("Choose the free product and its quantity"), field="free_product_id")
