"""Sales settings on products (invoicing policies, optional and accessory products) and customers
(pricelist)."""

from __future__ import annotations

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class ProductSale(Model):
    _extend = "product.product"

    invoice_policy = fields.Selection(
        [("order", "Ordered quantities"), ("delivery", "Delivered quantities")],
        "Invoicing policy",
        default="order",
        help="Invoice what the customer ordered, or only what was delivered",
    )
    service_policy = fields.Selection(
        [("manual", "Manually"), ("milestones", "By milestones")],
        "Service delivery",
        default="manual",
        help="How delivered quantities of a service are tracked (entered on the order line in both cases)",
    )
    optional_product_ids = fields.Many2many(
        "product.product",
        "Optional products",
        relation="product_optional_rel",
        column1="product_id",
        column2="optional_id",
        domain=[["is_sellable", "=", True]],
        help="Offered as optional lines when this product is added to a quotation",
    )
    accessory_product_ids = fields.Many2many(
        "product.product",
        "Accessories",
        relation="product_accessory_rel",
        column1="product_id",
        column2="accessory_id",
        domain=[["is_sellable", "=", True]],
        help="Suggested with this product (upsell)",
    )

    def _variant_shared_fields(self) -> list[str]:
        return [
            *super()._variant_shared_fields(),
            "invoice_policy",
            "service_policy",
            "optional_product_ids",
            "accessory_product_ids",
        ]

    @api.constrains("optional_product_ids", "accessory_product_ids")
    def _check_suggestions(self) -> None:
        for product in self:
            if product.ids[0] in product.optional_product_ids.ids + product.accessory_product_ids.ids:
                raise ValidationError(
                    _("A product cannot be its own optional product or accessory"), field="optional_product_ids"
                )


class PartnerSale(Model):
    _extend = "contacts.partner"

    pricelist_id = fields.Many2one(
        "product.pricelist",
        "Pricelist",
        ondelete="set_null",
        help="Prices for this customer (default: the pricelist of their country, else the company's)",
    )
    sale_order_count = fields.Integer("Sales orders", compute="_compute_sale_order_count")

    def _compute_sale_order_count(self) -> None:
        orders = self.env["sale.quotation"]
        for partner in self:
            partner.sale_order_count = (
                orders.search_count([("commercial_partner_id", "=", partner.ids[0]), ("state", "=", "accepted")])
                if orders.check_access_rights("read", raise_exception=False)
                else 0
            )
