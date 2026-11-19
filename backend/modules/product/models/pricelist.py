"""Pricelists and their rules.

``pricelist._get_product_price(product, quantity, uom, date, currency)`` returns the unit price of a
product. Among the rules that apply (minimum quantity reached, date inside the window), the most specific
wins: variant, then product (or the variant's template), then category (deepest first), then all products;
ties go to the higher minimum quantity, then to the sequence. Without a rule the sales price applies.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields
from tessera.orm import fields as F

from .product import round_to_step

APPLIED_ON = [
    ("all", "All products"),
    ("category", "Product category"),
    ("product", "Product"),
    ("variant", "Product variant"),
]
COMPUTE_PRICE = [("fixed", "Fixed price"), ("percentage", "Discount"), ("formula", "Formula")]
BASES = [("list_price", "Sales price"), ("standard_price", "Cost"), ("pricelist", "Other pricelist")]
_APPLIED_RANK = {"variant": 0, "product": 1, "category": 2, "all": 3}
_QTY_EPSILON = 1e-9


class Pricelist(Model):
    _name = "product.pricelist"
    _description = "Pricelist"
    _order = "sequence, id"
    _audit = True

    name = fields.Char(required=True, translate=True)
    currency_id = fields.Many2one(
        "core.currency",
        "Currency",
        required=True,
        ondelete="restrict",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).elevated().currency_id.ids[0],
    )
    company_id = fields.Many2one(
        "core.company",
        "Company",
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
        help="Empty = shared by every company",
    )
    country_ids = fields.Many2many(
        "core.country",
        "Countries",
        help="Customers in these countries get this pricelist unless they have one of their own",
    )
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    item_ids = fields.One2many("product.pricelist_item", "pricelist_id", "Rules", copy=True)

    # -- price computation -----------------------------------------------------------------------------
    def _get_product_price(
        self,
        product: Model,
        quantity: float = 1.0,
        uom: Model | None = None,
        date: dt.date | dt.datetime | str | None = None,
        currency: Model | None = None,
    ) -> float:
        """Unit price of ``product`` for ``quantity`` in ``uom`` (default: the product's unit) on ``date``,
        in ``currency`` (default: the pricelist's)."""
        return self._get_price_and_rule(product, quantity, uom=uom, date=date, currency=currency)[0]

    def _get_product_rule(
        self,
        product: Model,
        quantity: float = 1.0,
        uom: Model | None = None,
        date: dt.date | dt.datetime | str | None = None,
    ) -> Model:
        """The rule ``_get_product_price`` uses (an empty recordset when the sales price applies)."""
        return self._get_price_and_rule(product, quantity, uom=uom, date=date)[1]

    def _get_price_and_rule(
        self,
        product: Model,
        quantity: float = 1.0,
        uom: Model | None = None,
        date: dt.date | dt.datetime | str | None = None,
        currency: Model | None = None,
    ) -> tuple[float, Model]:
        self.ensure_one()
        product.ensure_one()
        self.check_access("read")  # the caller must see the pricelist; rules and costs are read elevated
        on = F.Date.to_date(date) or self.env.today()
        me = self.elevated("price computation")
        prod = product.elevated("price computation")
        qty = float(quantity or 0.0)
        if uom and prod.uom_id and uom.ids != prod.uom_id.ids:
            qty = uom._compute_quantity(qty, prod.uom_id, round=False)
        price, rule = me._compute_price(prod, qty, on, ())
        if uom and prod.uom_id and uom.ids != prod.uom_id.ids:
            price = prod.uom_id._compute_price(price, uom)
        target = currency or me.currency_id
        price = me._convert(price, me.currency_id, target, on)
        return float(target.round(price)), self.env["product.pricelist_item"].browse(rule.ids)

    def _convert(self, amount: float, from_currency: Model, to_currency: Model, on: dt.date) -> float:
        if not from_currency or not to_currency or from_currency.ids == to_currency.ids:
            return float(amount)
        company = self.company_id or self.env.company or None
        return float(from_currency.convert(amount, to_currency, company, on))

    def _compute_price(self, product: Model, qty: float, on: dt.date, seen: tuple[int, ...]) -> tuple[float, Model]:
        """(price in this pricelist's currency per unit of the product's unit, rule used)."""
        self.ensure_one()
        if self.ids[0] in seen:
            raise ValidationError(
                _("The pricelist '{name}' is based on itself through other pricelists", name=self.name),
                field="base_pricelist_id",
            )
        seen = (*seen, self.ids[0])
        rule = self._match_rule(product, qty, on)
        if not rule:
            return self._convert(product.list_price or 0.0, product.currency_id, self.currency_id, on), rule
        if rule.compute_price == "fixed":
            price = rule.fixed_price or 0.0
            if rule.applied_on == "product" and product.template_id and rule.product_id == product.template_id:
                # a template's fixed price is the price of its basic variant; value extras still apply
                price += self._convert(product.price_extra or 0.0, product.currency_id, self.currency_id, on)
            return price, rule
        base = self._base_price(rule, product, qty, on, seen)
        if rule.compute_price == "percentage":
            return base * (1 - (rule.percent_price or 0.0) / 100), rule
        price = base * (1 - (rule.price_discount or 0.0) / 100)
        if rule.price_round:
            price = round_to_step(price, rule.price_round)
        price += rule.price_surcharge or 0.0
        if rule.price_min_margin is not None or rule.price_max_margin is not None:
            cost = self._convert(product.standard_price or 0.0, product.currency_id, self.currency_id, on)
            if rule.price_min_margin is not None:
                price = max(price, cost + rule.price_min_margin)
            if rule.price_max_margin is not None:
                price = min(price, cost + rule.price_max_margin)
        return price, rule

    def _base_price(self, rule: Model, product: Model, qty: float, on: dt.date, seen: tuple[int, ...]) -> float:
        if rule.base == "pricelist" and rule.base_pricelist_id:
            other = rule.base_pricelist_id
            price, _rule = other._compute_price(product, qty, on, seen)
            return self._convert(price, other.currency_id, self.currency_id, on)
        amount = product.standard_price if rule.base == "standard_price" else product.list_price
        return self._convert(amount or 0.0, product.currency_id, self.currency_id, on)

    def _match_rule(self, product: Model, qty: float, on: dt.date) -> Model:
        """The most specific applicable rule of this pricelist for ``product``."""
        self.ensure_one()
        category_ids = product.category_id._ancestor_ids() if product.category_id else []
        product_ids = product.ids + (product.template_id.ids if product.template_id else [])
        targets: list[Any] = [("applied_on", "=", "all")]
        if category_ids:
            targets.append({"all": [("applied_on", "=", "category"), ("category_id", "in", category_ids)]})
        targets.append({"all": [("applied_on", "=", "product"), ("product_id", "in", product_ids)]})
        targets.append({"all": [("applied_on", "=", "variant"), ("variant_id", "=", product.ids[0])]})
        domain: list[Any] = [
            ("pricelist_id", "=", self.ids[0]),
            {"any": [("min_quantity", "is_not_set"), ("min_quantity", "<=", qty + _QTY_EPSILON)]},
            {"any": [("date_start", "is_not_set"), ("date_start", "<=", on)]},
            {"any": [("date_end", "is_not_set"), ("date_end", ">=", on)]},
            {"any": targets},
        ]
        items = self.env["product.pricelist_item"].elevated().search(domain)

        def rank(item: Model) -> tuple[Any, ...]:
            depth = category_ids.index(item.category_id.ids[0]) if item.applied_on == "category" else 0
            return (
                _APPLIED_RANK.get(item.applied_on, 9),
                depth,
                -(item.min_quantity or 0.0),
                item.sequence or 0,
                item.ids[0],
            )

        if not items:
            return items
        return min(items, key=rank)

    # -- choosing a pricelist ----------------------------------------------------------------------------
    def _for_partner(self, partner: Model | None, company: Model | None = None) -> Model:
        """The pricelist that applies to ``partner`` in ``company`` (default: the current company):

        1. the partner's own pricelist (``pricelist_id``, added by ``sale``) when it is shared or belongs to
           the company;
        2. else the first active pricelist whose countries contain the partner's country;
        3. else the company's default: the first pricelist of the company or shared, by sequence, preferring
           pricelists without countries.

        Returns an empty recordset when no pricelist is available (the sales price applies)."""
        company = company or self.env.company
        pricelists = self.env["product.pricelist"]
        company_domain: Any = (
            {"any": [("company_id", "is_not_set"), ("company_id", "=", company.ids[0])]}
            if company
            else ("company_id", "is_not_set")
        )
        if partner:
            partner.ensure_one()
            if "pricelist_id" in partner._fields:
                own = partner.pricelist_id
                if own and own.active and (not own.company_id or (company and own.company_id == company)):
                    return pricelists.browse(own.ids)
            country = partner.country_id
            if not country and "commercial_partner_id" in partner._fields:
                country = partner.commercial_partner_id.country_id
            if country:
                regional = pricelists.search([company_domain, ("country_ids", "in", country.ids)], limit=1)
                if regional:
                    return regional
        default = pricelists.search([company_domain, ("country_ids", "is_not_set")], limit=1)
        return default or pricelists.search([company_domain], limit=1)


class PricelistItem(Model):
    _name = "product.pricelist_item"
    _description = "Pricelist rule"
    _order = "pricelist_id, sequence, id"

    pricelist_id = fields.Many2one("product.pricelist", "Pricelist", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", "Company", related="pricelist_id.company_id", store=True)
    currency_id = fields.Many2one("core.currency", "Currency", related="pricelist_id.currency_id")
    sequence = fields.Integer(default=10)
    applied_on = fields.Selection(APPLIED_ON, "Applies to", required=True, default="all")
    category_id = fields.Many2one(
        "product.category", "Category", ondelete="cascade", help="Also applies to its sub-categories"
    )
    product_id = fields.Many2one(
        "product.product",
        "Product",
        ondelete="cascade",
        help="A single product, or a template: the rule then applies to every variant",
    )
    variant_id = fields.Many2one("product.product", "Variant", ondelete="cascade")
    min_quantity = fields.Float(
        "Minimum quantity", digits=(16, 3), default=0.0, help="In the product's unit; the rule applies from it"
    )
    date_start = fields.Date("Start date", help="First day the rule applies")
    date_end = fields.Date("End date", help="Last day the rule applies")
    compute_price = fields.Selection(COMPUTE_PRICE, "Price", required=True, default="fixed")
    fixed_price = fields.Monetary("Fixed price", currency_field="currency_id")
    percent_price = fields.Float("Discount %", help="Discount on the base price")
    base = fields.Selection(
        BASES, "Based on", required=True, default="list_price", help="Price the discount or formula starts from"
    )
    base_pricelist_id = fields.Many2one("product.pricelist", "Other pricelist", ondelete="restrict")
    price_discount = fields.Float("Formula discount %", help="Percentage taken off the base price")
    price_round = fields.Float(
        "Round to", digits=(16, 4), help="The discounted price is rounded to a multiple of this amount (e.g. 0.05, 1)"
    )
    price_surcharge = fields.Monetary(
        "Surcharge", currency_field="currency_id", help="Added after rounding (e.g. -0.01 for prices ending in .99)"
    )
    price_min_margin = fields.Monetary(
        "Minimum margin", currency_field="currency_id", help="The price is at least the cost plus this amount"
    )
    price_max_margin = fields.Monetary(
        "Maximum margin", currency_field="currency_id", help="The price is at most the cost plus this amount"
    )

    @api.depends("applied_on", "category_id", "product_id", "variant_id", "min_quantity")
    def _compute_display_name(self) -> None:
        for item in self:
            if item.applied_on == "category" and item.category_id:
                target = str(_("Category: {category}", category=item.category_id.display_name))
            elif item.applied_on == "product" and item.product_id:
                target = item.product_id.display_name
            elif item.applied_on == "variant" and item.variant_id:
                target = item.variant_id.display_name
            else:
                target = str(_("All products"))
            if item.min_quantity:
                target = str(_("{target} (from {quantity})", target=target, quantity=f"{item.min_quantity:g}"))
            item.display_name = target

    @api.constrains(
        "applied_on",
        "category_id",
        "product_id",
        "variant_id",
        "compute_price",
        "base",
        "base_pricelist_id",
        "date_start",
        "date_end",
        "min_quantity",
        "percent_price",
        "price_round",
        "price_min_margin",
        "price_max_margin",
    )
    def _check_rule(self) -> None:
        for item in self.elevated():
            if item.applied_on == "category" and not item.category_id:
                raise ValidationError(_("Choose the category the rule applies to"), field="category_id")
            if item.applied_on == "product" and not item.product_id:
                raise ValidationError(_("Choose the product the rule applies to"), field="product_id")
            if item.applied_on == "variant" and not item.variant_id:
                raise ValidationError(_("Choose the variant the rule applies to"), field="variant_id")
            if item.date_start and item.date_end and item.date_start > item.date_end:
                raise ValidationError(_("The rule ends before it starts"), field="date_end")
            if (item.min_quantity or 0) < 0:
                raise ValidationError(_("The minimum quantity cannot be negative"), field="min_quantity")
            if (item.percent_price or 0) > 100:
                raise ValidationError(_("A discount cannot exceed 100 %"), field="percent_price")
            if (item.price_round or 0) < 0:
                raise ValidationError(_("The rounding cannot be negative"), field="price_round")
            if (
                item.price_min_margin is not None
                and item.price_max_margin is not None
                and item.price_min_margin > item.price_max_margin
            ):
                raise ValidationError(_("The minimum margin is above the maximum margin"), field="price_max_margin")
            if item.compute_price != "fixed" and item.base == "pricelist":
                if not item.base_pricelist_id:
                    raise ValidationError(_("Choose the pricelist the rule is based on"), field="base_pricelist_id")
                item._check_base_recursion()

    def _check_base_recursion(self) -> None:
        """Following the base pricelists of non-fixed rules from this rule must never come back to its pricelist."""
        self.ensure_one()
        start = self.pricelist_id.ids[0]
        items = self.env["product.pricelist_item"].elevated()
        stack = list(self.base_pricelist_id.ids)
        seen: set[int] = set()
        while stack:
            current = stack.pop()
            if current == start:
                raise ValidationError(
                    _("A pricelist cannot be based on itself, even through other pricelists"),
                    field="base_pricelist_id",
                )
            if current in seen:
                continue
            seen.add(current)
            based = items.search(
                [
                    ("pricelist_id", "=", current),
                    ("base", "=", "pricelist"),
                    ("compute_price", "!=", "fixed"),
                    ("base_pricelist_id", "is_set"),
                ]
            )
            stack.extend(based.mapped("base_pricelist_id").ids)
