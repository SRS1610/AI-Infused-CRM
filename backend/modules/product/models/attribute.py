"""Product attributes (colour, size…), their values and the lines that give a template its attributes (D-071)."""

from __future__ import annotations

import re
from typing import Any

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

_HTML_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class Attribute(Model):
    _name = "product.attribute"
    _description = "Product attribute"
    _order = "sequence, id"

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    display_type = fields.Selection(
        [("radio", "Radio buttons"), ("select", "Drop-down list"), ("color", "Colour swatches")],
        "Display",
        required=True,
        default="radio",
        help="How customers choose a value",
    )
    value_ids = fields.One2many("product.attribute_value", "attribute_id", "Values", copy=True)


class AttributeValue(Model):
    _name = "product.attribute_value"
    _description = "Product attribute value"
    _order = "attribute_id, sequence, id"
    _sql_constraints = [
        ("value_uniq", "UNIQUE (attribute_id, code)", "Two values of an attribute cannot share a code"),
    ]

    attribute_id = fields.Many2one("product.attribute", "Attribute", required=True, ondelete="cascade")
    name = fields.Char("Value", required=True, translate=True)
    code = fields.Char(help="Short code added to the variants' internal reference (e.g. RED, XL)")
    sequence = fields.Integer(default=10)
    html_color = fields.Char("Colour", help="HTML colour shown for colour attributes, e.g. #c0392b")
    price_extra = fields.Float(
        "Extra price", digits=(16, 4), default=0.0, help="Added to the template's sales price for this value"
    )

    @api.constrains("html_color")
    def _check_color(self) -> None:
        for value in self:
            if value.html_color and not _HTML_COLOR.match(value.html_color):
                raise ValidationError(_("A colour is written #rgb or #rrggbb"), field="html_color")

    def write(self, vals: dict[str, Any]) -> bool:
        result = super().write(vals)
        if "price_extra" in vals:  # the variants' sales prices include the extra
            variants = (
                self.env["product.product"]
                .elevated()
                .with_context(active_test=False)
                .search([("attribute_value_ids", "in", self.ids)])
            )
            if variants:
                variants._sync_from_template(["list_price"])
        return result


class AttributeLine(Model):
    _name = "product.attribute_line"
    _description = "Product attribute line"
    _order = "product_id, sequence, id"
    _rec_name = "attribute_id"
    _sql_constraints = [
        ("attribute_uniq", "UNIQUE (product_id, attribute_id)", "An attribute is listed once per product"),
    ]

    product_id = fields.Many2one("product.product", "Template", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    attribute_id = fields.Many2one("product.attribute", "Attribute", required=True, ondelete="restrict")
    value_ids = fields.Many2many("product.attribute_value", "Values")

    @api.constrains("product_id", "attribute_id", "value_ids")
    def _check_values(self) -> None:
        for line in self.elevated():
            if line.product_id.template_id:
                raise ValidationError(
                    _("A variant cannot have attributes of its own: add them to its template"), field="product_id"
                )
            if not line.value_ids:
                raise ValidationError(_("Choose at least one value for each attribute"), field="value_ids")
            if line.value_ids.filtered(lambda v, line=line: v.attribute_id != line.attribute_id):
                raise ValidationError(
                    _("The values of an attribute line must belong to its attribute"), field="value_ids"
                )
