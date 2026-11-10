"""Quotations and their lines. States follow the ``sale.wf_quotation`` workflow:
draft → sent → accepted | rejected | expired, plus cancelled and back to draft."""

from __future__ import annotations

import datetime as dt
import secrets
from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

STATES = [
    ("draft", "Draft"),
    ("sent", "Sent"),
    ("accepted", "Accepted"),
    ("rejected", "Rejected"),
    ("expired", "Expired"),
    ("cancelled", "Cancelled"),
]
DEFAULT_VALIDITY_DAYS = 30


def _company(model: Model) -> Model:
    return model.env.company or model.env.ref("core.main_company")


class Quotation(Model):
    _name = "sale.quotation"
    _description = "Quotation"
    _order = "date_order desc, id desc"
    _inherit = ["chatter.thread"]
    _audit = True
    _global_search = ["name", "partner_id", "client_reference"]

    name = fields.Char("Number", readonly=True, copy=False, index=True, default="New")
    partner_id = fields.Many2one("contacts.partner", "Customer", required=True, ondelete="restrict", tracking=True)
    commercial_partner_id = fields.Many2one(
        "contacts.partner",
        related="partner_id.commercial_partner_id",
        store=True,
        index=True,
        string="Customer company",
    )
    client_reference = fields.Char("Customer reference")
    date_order = fields.Datetime("Quotation date", default=lambda self: self.env.now(), required=True)
    validity_date = fields.Date(
        "Valid until", default=lambda self: self.env.today() + dt.timedelta(days=DEFAULT_VALIDITY_DAYS), tracking=True
    )
    user_id = fields.Many2one(
        "core.user", "Salesperson", default=lambda self: self.env.uid, ondelete="set_null", tracking=True
    )
    company_id = fields.Many2one(
        "core.company", required=True, default=lambda self: _company(self).ids[0], ondelete="restrict"
    )
    currency_id = fields.Many2one(
        "core.currency",
        required=True,
        default=lambda self: _company(self).elevated().currency_id.ids[0],
        ondelete="restrict",
    )
    line_ids = fields.One2many("sale.quotation_line", "quotation_id", "Lines", copy=True)
    amount_untaxed = fields.Monetary("Untaxed", currency_field="currency_id", compute="_compute_amounts", store=True)
    amount_tax = fields.Monetary("Taxes", currency_field="currency_id", compute="_compute_amounts", store=True)
    amount_total = fields.Monetary(
        "Total", currency_field="currency_id", compute="_compute_amounts", store=True, tracking=True
    )
    note = fields.Html("Terms and conditions")
    state = fields.Selection(
        STATES, default="draft", required=True, readonly=True, tracking=True, copy=False, index=True
    )
    access_token = fields.Char(copy=False, groups=("sale.group_sale_user", "core.group_system"))
    sent_at = fields.Datetime(readonly=True, copy=False)
    signed_by = fields.Char(readonly=True, copy=False)
    signed_email = fields.Char(readonly=True, copy=False)
    signed_ip = fields.Char(readonly=True, copy=False)
    signed_at = fields.Datetime(readonly=True, copy=False)
    rejection_reason = fields.Text(readonly=True, copy=False)

    @api.depends("line_ids.price_subtotal", "line_ids.price_tax", "line_ids.is_optional", "line_ids.display_type")
    def _compute_amounts(self) -> None:
        for q in self:
            lines = q.line_ids.filtered(lambda line: not line.display_type and not line.is_optional)
            q.amount_untaxed = sum(lines.mapped("price_subtotal"))
            q.amount_tax = sum(lines.mapped("price_tax"))
            q.amount_total = q.amount_untaxed + q.amount_tax

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        sequences = self.env["core.sequence"].elevated()
        for vals in batch:
            if vals.get("name", "New") == "New":
                vals["name"] = sequences.next_by_code("sale.quotation")
        return super().create(batch)

    @api.constrains("validity_date", "date_order")
    def _check_dates(self) -> None:
        for q in self:
            if q.validity_date and q.date_order and q.validity_date < q.date_order.date():
                raise ValidationError(_("The validity date is before the quotation date"), field="validity_date")

    # -- workflow ------------------------------------------------------------------------------------

    def _transition(self, xmlid: str, *, elevated: bool = False) -> None:
        transition = self.env.ref(xmlid)
        for q in self:
            target = q.elevated(f"quotation {xmlid}") if elevated else q
            transition.with_env(target.env).apply(target)

    def portal_url(self) -> str:
        self.ensure_one()
        token = self.elevated().access_token
        return f"/portal/quotes/{self.ids[0]}?token={token}"

    @api.expose
    def action_send(self, email_to: str | None = None) -> dict[str, Any]:
        """Generate the portal token, attach the PDF, email the customer, move to *sent*."""
        from tessera.config import get_settings

        self.ensure_one()
        if not self.line_ids.filtered(lambda line: not line.display_type):
            raise UserError(_("Add at least one line before sending"))
        if self.state not in ("draft", "sent"):
            raise UserError(_("Only draft or sent quotations can be sent"))
        me = self.elevated("send quotation")
        if not me.access_token:
            me.write({"access_token": secrets.token_urlsafe(24)})
        recipient = email_to or self.partner_id.email or self.partner_id.commercial_partner_id.email
        if not recipient:
            raise UserError(_("The customer has no email address"))
        attachment_ids: list[int] = []
        template = self.env.ref("sale.report_quotation", raise_if_not_found=False)
        if template is not None:
            content, _mimetype, filename = template.render(self.ids, "pdf")
            att = (
                self.env["core.attachment"]
                .elevated()
                .create(
                    {
                        "name": filename,
                        "mimetype": "application/pdf",
                        "model": self._name,
                        "res_id": self.ids[0],
                        "raw": content,
                    }
                )
            )
            attachment_ids = att.ids
        base = get_settings().public_url.rstrip("/")
        link = f"{base}{self.portal_url()}"
        body = (
            f"<p>Hello {self.partner_id.name},</p><p>Please find our quotation <b>{self.name}</b> "
            f"for {self.amount_total:,.2f} {self.currency_id.name}, valid until {self.validity_date}.</p>"
            f'<p><a href="{link}">View and accept it online</a></p><p>{self.user_id.name or ""}</p>'
        )
        from modules.chatter.thread import reply_address

        self.env["core.mail_queue"].queue(
            {
                "email_to": recipient,
                "subject": f"Quotation {self.name}",
                "body_html": body,
                "reply_to": reply_address(self._name, self.ids[0]),
                "model": self._name,
                "res_id": self.ids[0],
                "attachment_ids": [("set", attachment_ids)],
            }
        )
        if self.state == "draft":
            self._transition("sale.tr_send")
        self.elevated().write({"sent_at": self.env.now()})
        from modules.chatter.thread import post_message

        post_message(
            self.env,
            self._name,
            self.ids[0],
            f"<p>Quotation sent to {recipient}.</p>",
            message_type="notification",
            attachment_ids=attachment_ids,
            notify=False,
        )
        return {"portal_url": link}

    def accept(self, signed_by: str, signed_email: str | None = None, ip: str | None = None) -> None:
        """Customer acceptance (portal or on behalf of the customer)."""
        self.ensure_one()
        if not (signed_by or "").strip():
            raise UserError(_("Type your full name to accept"))
        if self.state != "sent":
            raise UserError(_("This quotation can no longer be accepted"))
        if self.validity_date and self.validity_date < self.env.today():
            raise UserError(_("This quotation has expired"))
        self.elevated().write(
            {"signed_by": signed_by.strip(), "signed_email": signed_email, "signed_ip": ip, "signed_at": self.env.now()}
        )
        self._transition("sale.tr_accept", elevated=True)

    def reject(self, reason: str | None = None) -> None:
        self.ensure_one()
        if self.state != "sent":
            raise UserError(_("This quotation can no longer be rejected"))
        self.elevated().write({"rejection_reason": reason})
        self._transition("sale.tr_reject", elevated=True)

    @api.expose
    def action_accept(self, signed_by: str) -> bool:
        self.accept(signed_by, ip=None)
        return True

    @api.expose
    def action_reject(self, reason: str | None = None) -> bool:
        self.reject(reason)
        return True

    @api.expose
    def action_cancel(self) -> bool:
        self._transition("sale.tr_cancel")
        return True

    @api.expose
    def action_draft(self) -> bool:
        self._transition("sale.tr_draft")
        return True

    @api.expose
    def action_expire(self) -> bool:
        """Called by the expiry automation (validity date passed)."""
        for q in self.filtered(lambda r: r.state == "sent"):
            q._transition("sale.tr_expire", elevated=True)
        return True


class QuotationLine(Model):
    _name = "sale.quotation_line"
    _description = "Quotation line"
    _order = "quotation_id, sequence, id"
    _rec_name = "name"

    quotation_id = fields.Many2one("sale.quotation", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    display_type = fields.Selection([("section", "Section"), ("note", "Note")])
    product_id = fields.Many2one("product.product", ondelete="restrict")
    name = fields.Text("Description", required=True)
    quantity = fields.Float(default=1.0, digits=(16, 3))
    uom_id = fields.Many2one("product.uom", "Unit", ondelete="set_null")
    price_unit = fields.Float("Unit price", digits=(16, 4))
    discount = fields.Float("Discount %", default=0.0)
    tax_percent = fields.Float("Tax %", default=0.0)
    is_optional = fields.Boolean("Optional", help="Offered as an option; not in the totals until accepted")
    currency_id = fields.Many2one("core.currency", related="quotation_id.currency_id", store=True)
    price_subtotal = fields.Monetary(currency_field="currency_id", compute="_compute_amounts", store=True)
    price_tax = fields.Monetary(currency_field="currency_id", compute="_compute_amounts", store=True)
    price_total = fields.Monetary(currency_field="currency_id", compute="_compute_amounts", store=True)

    @api.depends("quantity", "price_unit", "discount", "tax_percent", "display_type")
    def _compute_amounts(self) -> None:
        for line in self:
            if line.display_type:
                line.price_subtotal = line.price_tax = line.price_total = 0.0
                continue
            subtotal = (line.quantity or 0) * (line.price_unit or 0) * (1 - (line.discount or 0) / 100)
            tax = subtotal * (line.tax_percent or 0) / 100
            line.price_subtotal, line.price_tax, line.price_total = subtotal, tax, subtotal + tax

    @api.constrains("discount", "quantity", "display_type")
    def _check_values(self) -> None:
        for line in self:
            if not 0 <= (line.discount or 0) <= 100:
                raise ValidationError(_("A discount is between 0 and 100 %"), field="discount")
            if not line.display_type and (line.quantity or 0) < 0:
                raise ValidationError(_("Quantities cannot be negative"), field="quantity")

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        products = self.env["product.product"].elevated()
        for vals in batch:  # fill description, unit, price and tax from the product
            if vals.get("product_id") and not vals.get("display_type"):
                product = products.browse([vals["product_id"]])
                vals.setdefault("name", product.description_sale or product.display_name)
                vals.setdefault("price_unit", product.list_price)
                vals.setdefault("tax_percent", product.tax_percent)
                if product.uom_id:
                    vals.setdefault("uom_id", product.uom_id.ids[0])
        return super().create(batch)
