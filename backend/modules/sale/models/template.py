"""Quotation templates: the lines, optional products, validity and terms a quotation starts from."""

from __future__ import annotations

from typing import Any

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class OrderTemplate(Model):
    _name = "sale.order_template"
    _description = "Quotation template"
    _order = "sequence, name, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "core.company",
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
        help="Empty = shared by every company",
    )
    number_of_days = fields.Integer(
        "Validity (days)", default=30, help="The quotation is valid this many days after the template is applied"
    )
    payment_term_id = fields.Many2one("account.payment_term", "Payment terms", ondelete="set_null")
    note = fields.Html("Terms and conditions", translate=True)
    line_ids = fields.One2many("sale.order_template_line", "template_id", "Lines", copy=True)
    option_ids = fields.One2many("sale.order_template_option", "template_id", "Optional products", copy=True)

    @api.constrains("number_of_days")
    def _check_days(self) -> None:
        for template in self:
            if (template.number_of_days or 0) < 0:
                raise ValidationError(_("The validity cannot be negative"), field="number_of_days")

    def _line_values(self) -> list[dict[str, Any]]:
        """Quotation line values (template lines, then the options as optional lines)."""
        self.ensure_one()
        out: list[dict[str, Any]] = []
        for line in self.line_ids.sorted(lambda r: (r.sequence or 0, r.ids[0])):
            vals: dict[str, Any] = {"sequence": line.sequence or 10, "name": line.name}
            if line.display_type:
                vals["display_type"] = line.display_type
            else:
                vals.update(
                    {
                        "product_id": line.product_id.ids[0] if line.product_id else None,
                        "quantity": line.quantity,
                        "discount": line.discount or 0.0,
                    }
                )
                if line.uom_id:
                    vals["uom_id"] = line.uom_id.ids[0]
                if not line.name and line.product_id:
                    del vals["name"]  # the product's description
                if not line.product_id and not line.name:
                    continue
            out.append(vals)
        base = max([v.get("sequence", 10) for v in out], default=10)
        for i, option in enumerate(self.option_ids.sorted(lambda r: (r.sequence or 0, r.ids[0]))):
            vals = {
                "sequence": base + 100 + i,
                "product_id": option.product_id.ids[0],
                "quantity": option.quantity,
                "discount": option.discount or 0.0,
                "is_optional": True,
            }
            if option.name:
                vals["name"] = option.name
            if option.uom_id:
                vals["uom_id"] = option.uom_id.ids[0]
            out.append(vals)
        return out


class OrderTemplateLine(Model):
    _name = "sale.order_template_line"
    _description = "Quotation template line"
    _order = "template_id, sequence, id"

    template_id = fields.Many2one("sale.order_template", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="template_id.company_id", store=True)
    sequence = fields.Integer(default=10)
    display_type = fields.Selection([("section", "Section"), ("note", "Note")])
    product_id = fields.Many2one("product.product", "Product", ondelete="restrict", domain=[["is_sellable", "=", True]])
    name = fields.Text("Description", translate=True, help="Empty = the product's sales description")
    quantity = fields.Float(default=1.0, digits=(16, 3))
    uom_id = fields.Many2one("product.uom", "Unit", ondelete="set_null")
    discount = fields.Float("Discount %", default=0.0)

    @api.constrains("product_id", "name", "display_type", "quantity")
    def _check_line(self) -> None:
        for line in self:
            if line.display_type and not line.name:
                raise ValidationError(_("A section or note needs a text"), field="name")
            if not line.display_type and not line.product_id and not line.name:
                raise ValidationError(_("Choose a product or write a description"), field="product_id")
            if (line.quantity or 0) < 0:
                raise ValidationError(_("Quantities cannot be negative"), field="quantity")


class OrderTemplateOption(Model):
    _name = "sale.order_template_option"
    _description = "Quotation template optional product"
    _order = "template_id, sequence, id"

    template_id = fields.Many2one("sale.order_template", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="template_id.company_id", store=True)
    sequence = fields.Integer(default=10)
    product_id = fields.Many2one(
        "product.product", "Product", required=True, ondelete="restrict", domain=[["is_sellable", "=", True]]
    )
    name = fields.Text("Description", translate=True, help="Empty = the product's sales description")
    quantity = fields.Float(default=1.0, digits=(16, 3))
    uom_id = fields.Many2one("product.uom", "Unit", ondelete="set_null")
    discount = fields.Float("Discount %", default=0.0)
