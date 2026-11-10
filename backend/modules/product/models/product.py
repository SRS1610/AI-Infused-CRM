"""Products, categories and units of measure."""

from __future__ import annotations

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class Uom(Model):
    _name = "product.uom"
    _description = "Unit of measure"
    _order = "category, name"

    name = fields.Char(required=True, translate=True)
    category = fields.Selection(
        [("unit", "Unit"), ("time", "Working time"), ("weight", "Weight"), ("length", "Length"), ("volume", "Volume")],
        required=True,
        default="unit",
    )
    factor = fields.Float(default=1.0, help="How many of this unit make the category's reference unit")
    active = fields.Boolean(default=True)


class Category(Model):
    _name = "product.category"
    _description = "Product category"
    _order = "complete_name"
    _rec_name = "complete_name"

    name = fields.Char(required=True, translate=True)
    parent_id = fields.Many2one("product.category", ondelete="cascade")
    complete_name = fields.Char(compute="_compute_complete_name", store=True)

    @api.depends("name", "parent_id.complete_name")
    def _compute_complete_name(self) -> None:
        for cat in self:
            cat.complete_name = f"{cat.parent_id.complete_name} / {cat.name}" if cat.parent_id else cat.name


class Product(Model):
    _name = "product.product"
    _description = "Product"
    _order = "name, id"
    _audit = True
    _global_search = ["name", "default_code", "barcode"]
    _sql_constraints = [("barcode_uniq", "UNIQUE (barcode)", "A barcode can only be assigned to one product")]

    name = fields.Char(required=True, translate=True, index="trigram")
    default_code = fields.Char("Internal reference", index=True)
    barcode = fields.Char(copy=False)
    type = fields.Selection(
        [("service", "Service"), ("consumable", "Consumable"), ("goods", "Goods")], default="goods", required=True
    )
    category_id = fields.Many2one("product.category", "Category", ondelete="set_null")
    description_sale = fields.Text("Sales description", translate=True)
    uom_id = fields.Many2one(
        "product.uom",
        "Unit",
        ondelete="restrict",
        default=lambda self: self.env.ref("product.uom_unit", raise_if_not_found=False),
    )
    currency_id = fields.Many2one(
        "core.currency",
        default=lambda self: (self.env.company or self.env.ref("core.main_company")).elevated().currency_id.ids[0],
        ondelete="restrict",
    )
    list_price = fields.Monetary("Sales price", currency_field="currency_id", default=0.0)
    standard_price = fields.Monetary(
        "Cost", currency_field="currency_id", default=0.0, groups=("product.group_product_manager", "core.group_system")
    )
    tax_percent = fields.Float("Tax %", default=0.0, help="Placeholder until accounting taxes (Phase 2)")
    image = fields.Image(attachment=True, max_size=(1024, 1024))
    sale_ok = fields.Boolean("Can be sold", default=True)
    company_id = fields.Many2one("core.company", ondelete="set_null", help="Empty = shared by every company")
    active = fields.Boolean(default=True)

    @api.constrains("list_price", "tax_percent")
    def _check_prices(self) -> None:
        for p in self:
            if (p.list_price or 0) < 0:
                raise ValidationError(_("The sales price cannot be negative"), field="list_price")
            if not 0 <= (p.tax_percent or 0) <= 100:
                raise ValidationError(_("Tax must be between 0 and 100 %"), field="tax_percent")

    @api.depends("name", "default_code")
    def _compute_display_name(self) -> None:
        for p in self:
            p.display_name = f"[{p.default_code}] {p.name}" if p.default_code else p.name
