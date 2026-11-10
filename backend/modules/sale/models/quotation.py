"""Quotations / sales orders and their lines (D-011, D-074).

``sale.quotation`` is the sales document: a quotation becomes a sales order in place when the customer
accepts it online or a salesperson confirms it (state ``accepted``, labelled *Sales order*). States follow
the ``sale.wf_quotation`` workflow: draft → sent → accepted | rejected | expired, plus cancelled and back to
draft; ``locked`` freezes a fully invoiced order.

Prices and taxes (sale 2.0): the customer gives the pricelist (``product.pricelist._for_partner``), the
fiscal position and the payment terms; a line's unit price comes from the pricelist unless the user typed
one (``price_manual``), its taxes from the product's customer taxes in the order's company mapped by the
fiscal position, and its amounts from ``account.tax.compute_all``.

Invoicing quantities: ``qty_invoiced`` counts the linked customer invoice lines (draft or posted, not
cancelled; credit notes subtract), ``qty_to_invoice`` is what the invoicing policy allows minus returns minus
what is already invoiced (negative: a credit is due).
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections import defaultdict
from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

STATES = [
    ("draft", "Draft"),
    ("sent", "Sent"),
    ("accepted", "Sales order"),
    ("rejected", "Rejected"),
    ("expired", "Expired"),
    ("cancelled", "Cancelled"),
]
INVOICE_STATUS = [
    ("no", "Nothing to invoice"),
    ("to_invoice", "To invoice"),
    ("invoiced", "Fully invoiced"),
    ("upselling", "Upselling opportunity"),
]
DEFAULT_VALIDITY_DAYS = 30
EDITABLE = ("draft", "sent")
QTY_EPS = 1e-6
# context key set by the module's own writes (templates, promotions, invoicing, migration): skips the lock
# checks and the automatic repricing of lines
INTERNAL = "_sale_internal"
# what a locked order still accepts
LOCKED_ORDER_FIELDS = {
    "partner_id",
    "pricelist_id",
    "currency_id",
    "fiscal_position_id",
    "payment_term_id",
    "company_id",
    "line_ids",
    "date_order",
    "template_id",
    "promotion_code_ids",
}
LOCKED_LINE_WRITABLE = {"qty_delivered", "qty_returned", "sequence"}
# links to invoices are shown to users who may read invoices
INVOICE_LINE_GROUPS = ("account.group_account_invoice", "account.group_account_readonly")


def _company(model: Model) -> Model:
    return model.env.company or model.env.ref("core.main_company")


def _batch(vals_list: Any) -> list[dict[str, Any]]:
    return [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]


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
    confirmation_date = fields.Datetime(
        "Confirmed on", readonly=True, copy=False, index=True, help="When the quotation became a sales order"
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
        help="The pricelist's currency",
    )
    pricelist_id = fields.Many2one(
        "product.pricelist",
        "Pricelist",
        ondelete="restrict",
        tracking=True,
        help="Gives the unit prices of the lines (default: the customer's pricelist)",
    )
    fiscal_position_id = fields.Many2one(
        "account.fiscal_position",
        "Fiscal position",
        ondelete="restrict",
        help="Maps the products' taxes (default: the customer's, or the one detected from their country)",
    )
    payment_term_id = fields.Many2one("account.payment_term", "Payment terms", ondelete="restrict")
    template_id = fields.Many2one(
        "sale.order_template", "Quotation template", ondelete="set_null", help="Lines, options and terms to start from"
    )
    origin_order_id = fields.Many2one(
        "sale.quotation", "Original order", ondelete="set_null", copy=False, help="The order this one renews or returns"
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
    locked = fields.Boolean(
        readonly=True, copy=False, tracking=True, help="A locked order cannot be changed (it is fully invoiced)"
    )
    invoice_status = fields.Selection(
        INVOICE_STATUS,
        "Invoicing",
        compute="_compute_invoice_status",
        store=True,
        index=True,
        copy=False,
    )
    amount_to_invoice = fields.Monetary(
        "Untaxed to invoice",
        currency_field="currency_id",
        compute="_compute_invoice_status",
        store=True,
        copy=False,
    )
    invoice_ids = fields.Many2many("account.move", "Invoices", compute="_compute_invoices", groups=INVOICE_LINE_GROUPS)
    invoice_count = fields.Integer("Invoices", compute="_compute_invoices")
    promotion_code_ids = fields.Many2many(
        "sale.promotion",
        "Promotion codes",
        relation="sale_quotation_promotion_code_rel",
        column1="quotation_id",
        column2="promotion_id",
        copy=False,
        help="Code promotions entered for this order; they apply whenever the order is eligible",
    )
    promotion_ids = fields.Many2many(
        "sale.promotion", "Applied promotions", compute="_compute_promotions", help="Promotions with reward lines"
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

    @api.depends("state", "line_ids.invoice_status", "line_ids.untaxed_amount_to_invoice")
    def _compute_invoice_status(self) -> None:
        for q in self:
            lines = q.line_ids.filtered(lambda line: not line.display_type and not line.is_optional)
            q.amount_to_invoice = sum(lines.mapped("untaxed_amount_to_invoice")) if q.state == "accepted" else 0.0
            statuses = [line.invoice_status for line in lines if not line.is_downpayment]
            if q.state != "accepted":
                q.invoice_status = "no"
            elif "to_invoice" in statuses:
                q.invoice_status = "to_invoice"
            elif "upselling" in statuses:
                q.invoice_status = "upselling"
            elif statuses and all(s == "invoiced" for s in statuses):
                q.invoice_status = "invoiced"
            else:
                q.invoice_status = "no"

    def _compute_invoices(self) -> None:
        items = self.env["account.move_line"].elevated()
        for q in self:
            line_ids = q.elevated().line_ids.ids
            move_ids = items.search([("sale_line_ids", "in", line_ids)]).mapped("move_id").ids if line_ids else []
            q.invoice_ids = self.env["account.move"].browse(move_ids)
            q.invoice_count = len(move_ids)

    def _compute_promotions(self) -> None:
        for q in self:
            promos = q.line_ids.filtered(lambda line: line.is_reward_line).mapped("promotion_id")
            q.promotion_ids = self.env["sale.promotion"].browse(promos.ids)

    # -- defaults from the customer ---------------------------------------------------------------------

    def _partner_defaults(self, partner: Model, company: Model) -> dict[str, Any]:
        """Pricelist, currency, fiscal position and payment terms for ``partner`` in ``company``."""
        partner = partner.elevated()
        pricelist = self.env["product.pricelist"].with_company(company)._for_partner(partner, company)
        position = self.env["account.fiscal_position"]._get_for_partner(partner, company)
        term = partner._accounting(company, "property_payment_term_id")
        out: dict[str, Any] = {
            "pricelist_id": pricelist.ids[0] if pricelist else None,
            "fiscal_position_id": position.ids[0] if position else None,
            "payment_term_id": term.ids[0] if term else None,
        }
        if pricelist:
            out["currency_id"] = pricelist.elevated().currency_id.ids[0]
        return out

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = _batch(vals_list)
        sequences = self.env["core.sequence"].elevated()
        companies = self.env["core.company"]
        with_template: list[int] = []
        given = [set(vals) for vals in batch]  # what the caller chose (a template does not override it)
        for index, vals in enumerate(batch):
            if vals.get("name", "New") == "New":
                vals["name"] = sequences.next_by_code("sale.quotation")
            if vals.get("partner_id"):
                company = companies.browse([int(vals["company_id"])]) if vals.get("company_id") else _company(self)
                partner = self.env["contacts.partner"].browse([int(vals["partner_id"])])
                for key, value in self._partner_defaults(partner, company).items():
                    if key == "currency_id" and vals.get("pricelist_id"):
                        continue  # follows the given pricelist below
                    vals.setdefault(key, value)
            if vals.get("pricelist_id") and "currency_id" not in vals:
                pricelist = self.env["product.pricelist"].elevated().browse([int(vals["pricelist_id"])])
                vals["currency_id"] = pricelist.currency_id.ids[0]
            if vals.get("template_id") and not vals.get("line_ids"):
                with_template.append(index)
        # optional products are suggested once every line exists (not line by line)
        created = super(Quotation, self.with_context(sale_no_suggestions=True)).create(batch)
        records = self.browse(created.ids)
        for index in with_template:
            order = records.browse([records.ids[index]])
            order._apply_template(order.template_id, keep_terms=given[index])
        if not self.env.context.get("sale_no_suggestions"):
            plain = [rid for i, rid in enumerate(records.ids) if i not in with_template]
            records.browse(plain).mapped("line_ids")._add_optional_suggestions()
        return records

    def write(self, vals: dict[str, Any]) -> bool:
        vals = dict(vals)
        internal = bool(self.env.context.get(INTERNAL))
        if not internal:
            frozen = LOCKED_ORDER_FIELDS & set(vals)
            for q in self:
                if q.locked and frozen:
                    raise UserError(_("Sales order {name} is locked: unlock it to change it", name=q.name))
            if vals.get("state") == "cancelled":
                for q in self:
                    q._check_cancellable()
        if vals.get("state") == "accepted" and "confirmation_date" not in vals:
            vals["confirmation_date"] = self.env.now()
        if "partner_id" in vals and vals["partner_id"] and not internal:
            partner = self.env["contacts.partner"].browse([int(vals["partner_id"])])
            for q in self:
                defaults = q._partner_defaults(partner, q.company_id)
                own = {k: v for k, v in defaults.items() if k not in vals}
                if vals.get("pricelist_id"):
                    own.pop("currency_id", None)
                if own and q.state in EDITABLE:
                    super(Quotation, q).write(own)
                    q._after_pricing_change(set(own))
        if vals.get("pricelist_id") and "currency_id" not in vals:
            pricelist = self.env["product.pricelist"].elevated().browse([int(vals["pricelist_id"])])
            vals["currency_id"] = pricelist.currency_id.ids[0]
        if "line_ids" in vals and not self.env.context.get("sale_no_suggestions"):
            # optional products are suggested once every new line exists (not line by line)
            before = set(self.elevated().mapped("line_ids").ids)
            result = super(Quotation, self.with_context(sale_no_suggestions=True)).write(vals)
            added = self.elevated().mapped("line_ids").filtered(lambda line: line.ids[0] not in before)
            self.env["sale.quotation_line"].browse(added.ids)._add_optional_suggestions()
        else:
            result = super().write(vals)
        if not internal:
            self._after_pricing_change(set(vals))
        return result

    def _after_pricing_change(self, changed: set[str]) -> None:
        """Reprice lines after a pricelist change and remap their taxes after a fiscal position change."""
        if not changed & {"pricelist_id", "fiscal_position_id", "currency_id"}:
            return
        for q in self:
            if q.state not in EDITABLE and not (q.state == "accepted" and not q.locked):
                continue
            lines = q.line_ids.filtered(
                lambda line: line.product_id and not line.display_type and not line.is_downpayment
            )
            if changed & {"pricelist_id", "currency_id"}:
                lines.filtered(lambda line: not line.price_manual and not line.is_reward_line)._update_price()
            if "fiscal_position_id" in changed:
                lines.filtered(lambda line: not line.is_reward_line)._update_taxes()

    def _check_cancellable(self) -> None:
        if self.locked:
            raise UserError(_("Sales order {name} is locked: unlock it to change it", name=self.name))
        invoiced = self.line_ids.filtered(lambda line: abs(line.qty_invoiced or 0.0) > QTY_EPS)
        if invoiced:
            raise UserError(
                _("Sales order {name} has invoices: cancel or credit them before cancelling the order", name=self.name)
            )

    def _price_for(self, product: Model, quantity: float, uom: Model | None = None) -> float:
        """Unit price of ``product`` on this order (pricelist, else the sales price in the order's currency)."""
        self.ensure_one()
        me = self.elevated("price computation")
        product = product.elevated()
        if me.pricelist_id:
            return me.pricelist_id._get_product_price(
                product, quantity or 1.0, uom=uom, date=me.date_order, currency=me.currency_id
            )
        price = float(product.list_price or 0.0)
        if uom and product.uom_id and uom.ids != product.uom_id.ids:
            price = product.uom_id._compute_price(price, uom)
        if product.currency_id and me.currency_id and product.currency_id.ids != me.currency_id.ids:
            price = product.currency_id.convert(price, me.currency_id, me.company_id, me.date_order.date())
        return float(me.currency_id.round(price))

    def _taxes_for_product(self, product: Model) -> Model:
        """The product's customer taxes in this order's company, mapped by its fiscal position."""
        self.ensure_one()
        me = self.elevated()
        taxes = product.elevated()._taxes_for(me.company_id, "sale")
        if me.fiscal_position_id:
            taxes = me.fiscal_position_id.map_tax(taxes.elevated())
        return self.env["account.tax"].browse(taxes.ids)

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
            {
                "signed_by": signed_by.strip(),
                "signed_email": signed_email,
                "signed_ip": ip,
                "signed_at": self.env.now(),
                "confirmation_date": self.env.now(),
            }
        )
        self._transition("sale.tr_accept", elevated=True)

    def reject(self, reason: str | None = None) -> None:
        self.ensure_one()
        if self.state != "sent":
            raise UserError(_("This quotation can no longer be rejected"))
        self.elevated().write({"rejection_reason": reason})
        self._transition("sale.tr_reject", elevated=True)

    def add_optional_line(self, line_id: int) -> Model:
        """The customer adds an optional line to the quotation (portal): it counts in the totals from now on."""
        self.ensure_one()
        if self.state != "sent":
            raise UserError(_("Options can only be added to a quotation waiting for your answer"))
        if self.validity_date and self.validity_date < self.env.today():
            raise UserError(_("This quotation has expired"))
        line = self.line_ids.filtered(lambda r: r.ids[0] == line_id and r.is_optional)
        if not line:
            raise UserError(_("This option is not available"))
        line.with_context(**{INTERNAL: True}).write({"is_optional": False})
        from modules.chatter.thread import post_message

        post_message(
            self.env,
            self._name,
            self.ids[0],
            f"<p>The customer added the option: {line.name}</p>",
            message_type="notification",
            notify=False,
        )
        return line

    @api.expose
    def action_confirm(self) -> bool:
        """Confirm the quotation as a sales order without the customer's signature."""
        for q in self:
            if q.state not in EDITABLE:
                raise UserError(_("Only draft or sent quotations can be confirmed ({name})", name=q.name))
            if not q.line_ids.filtered(lambda line: not line.display_type and not line.is_optional):
                raise UserError(_("Add at least one line before confirming"))
            q.elevated().write({"confirmation_date": self.env.now()})
            q._transition("sale.tr_confirm")
        return True

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

    @api.expose
    def action_lock(self) -> bool:
        for q in self:
            if q.state != "accepted":
                raise UserError(_("Only sales orders can be locked ({name})", name=q.name))
        self.check_access("write")
        self.with_context(**{INTERNAL: True}).write({"locked": True})
        return True

    @api.expose
    def action_unlock(self) -> bool:
        self.check_access("write")
        self.with_context(**{INTERNAL: True}).write({"locked": False})
        return True

    # -- templates, promotions, invoicing (see the dedicated modules) -------------------------------------

    @api.expose
    def action_apply_template(self, template_id: int | None = None) -> bool:
        """Replace the lines with those of ``template_id`` (default: the order's template)."""
        for q in self:
            template = self.env["sale.order_template"].browse([int(template_id)]) if template_id else q.template_id
            if not template:
                raise UserError(_("Choose a quotation template first"))
            if q.state not in EDITABLE:
                raise UserError(_("Templates can only be applied to draft or sent quotations"))
            if template_id:
                q.write({"template_id": template.ids[0]})
            q._apply_template(template)
        return True

    def _apply_template(self, template: Model, keep_terms: set[str] | None = None) -> None:
        self.ensure_one()
        template = template.elevated()
        keep = keep_terms or set()
        me = self.with_context(**{INTERNAL: True})
        commands: list[Any] = [("delete", line_id) for line_id in self.line_ids.ids]
        commands += [("create", vals) for vals in template._line_values()]
        updates: dict[str, Any] = {"line_ids": commands}
        if template.number_of_days and "validity_date" not in keep:
            updates["validity_date"] = self.env.today() + dt.timedelta(days=template.number_of_days)
        if template.note and "note" not in keep:
            updates["note"] = template.note
        if template.payment_term_id and "payment_term_id" not in keep:
            updates["payment_term_id"] = template.payment_term_id.ids[0]
        me.write(updates)

    @api.expose
    def action_apply_promotions(self, code: str | None = None) -> dict[str, Any]:
        """Add the reward lines of every promotion the order is eligible for (and of ``code``); reward lines of
        promotions no longer eligible are removed. Applying again changes nothing."""
        from modules.sale.promotions import apply_promotions

        return apply_promotions(self, code=code)

    @api.expose
    def action_create_invoice(self, method: str = "delivered", amount: float | None = None) -> dict[str, Any]:
        """Invoice these orders: ``delivered`` invoices what the invoicing policy allows (and deducts down
        payments); ``percentage`` / ``fixed`` create a down payment invoice of ``amount`` (% of the total, or a
        tax-included amount)."""
        from modules.sale.invoicing import create_downpayment_invoices, create_invoices

        if method == "delivered":
            moves = create_invoices(self)
        elif method in ("percentage", "fixed"):
            if amount is None:
                raise UserError(_("Give the down payment amount"))
            moves = create_downpayment_invoices(self, method, float(amount))
        else:
            raise UserError(_("Unknown invoicing method '{method}'", method=method))
        return {"invoice_ids": moves.ids}

    @api.model
    @api.expose
    def action_invoice_orders(self, order_ids: list[int], method: str = "delivered") -> dict[str, Any]:
        """Invoice several orders at once (one invoice per customer and currency)."""
        return self.browse([int(i) for i in order_ids]).action_create_invoice(method)

    @api.expose
    def action_create_credit_note(self) -> dict[str, Any]:
        """Credit the returned quantities that were already invoiced."""
        from modules.sale.invoicing import create_return_credit_notes

        return {"invoice_ids": create_return_credit_notes(self).ids}

    def _suggested_products(self) -> Model:
        """Optional and accessory products of the order's products that are not on the order yet (upsell
        candidates; also used by the AI suggestions of sub-phase 2.6)."""
        self.ensure_one()
        lines = self.elevated().line_ids
        present = set(lines.mapped("product_id").ids)
        out: list[int] = []
        for product in lines.filtered(lambda line: not line.is_optional).mapped("product_id"):
            for candidate in product.optional_product_ids | product.accessory_product_ids:
                cid = candidate.ids[0]
                if cid not in present and cid not in out and candidate.is_sellable and candidate.active:
                    out.append(cid)
        return self.env["product.product"].browse(out)


class QuotationLine(Model):
    _name = "sale.quotation_line"
    _description = "Quotation line"
    _order = "quotation_id, sequence, id"
    _rec_name = "name"

    quotation_id = fields.Many2one("sale.quotation", required=True, ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", related="quotation_id.company_id", store=True, index=True)
    order_state = fields.Selection(STATES, related="quotation_id.state", string="Order state")
    sequence = fields.Integer(default=10)
    display_type = fields.Selection([("section", "Section"), ("note", "Note")])
    product_id = fields.Many2one("product.product", ondelete="restrict", domain=[["is_sellable", "=", True]])
    name = fields.Text("Description", required=True)
    quantity = fields.Float(default=1.0, digits=(16, 3))
    uom_id = fields.Many2one("product.uom", "Unit", ondelete="set_null")
    price_unit = fields.Float("Unit price", digits=(16, 4))
    price_manual = fields.Boolean(
        "Price typed", copy=True, help="The unit price was typed: the pricelist no longer changes it"
    )
    discount = fields.Float("Discount %", default=0.0)
    tax_ids = fields.Many2many(
        "account.tax",
        "Taxes",
        relation="sale_quotation_line_tax_rel",
        column1="line_id",
        column2="tax_id",
        domain=[["type_tax_use", "=", "sale"]],
    )
    is_optional = fields.Boolean("Optional", help="Offered as an option; not in the totals until accepted")
    is_downpayment = fields.Boolean("Down payment", readonly=True, copy=False)
    is_reward_line = fields.Boolean("Promotion reward", readonly=True)
    promotion_id = fields.Many2one("sale.promotion", "Promotion", readonly=True, ondelete="set_null")
    promotion_reward_id = fields.Many2one("sale.promotion_reward", "Reward", readonly=True, ondelete="set_null")
    currency_id = fields.Many2one("core.currency", related="quotation_id.currency_id", store=True)
    price_subtotal = fields.Monetary(currency_field="currency_id", compute="_compute_amounts", store=True)
    price_tax = fields.Monetary(currency_field="currency_id", compute="_compute_amounts", store=True)
    price_total = fields.Monetary(currency_field="currency_id", compute="_compute_amounts", store=True)
    # -- delivery and invoicing ------------------------------------------------------------------------
    invoice_policy = fields.Selection(
        [("order", "Ordered quantities"), ("delivery", "Delivered quantities")],
        "Invoicing policy",
        compute="_compute_policies",
        store=True,
    )
    qty_delivered_method = fields.Selection(
        [("manual", "Manual"), ("milestones", "Milestones")],
        "Delivery tracking",
        compute="_compute_policies",
        store=True,
        help="How the delivered quantity is known (entered by hand until stock arrives in Phase 3)",
    )
    qty_delivered = fields.Float("Delivered", digits=(16, 3), default=0.0, copy=False)
    qty_returned = fields.Float(
        "Returned", digits=(16, 3), default=0.0, copy=False, help="Quantity the customer sent back"
    )
    invoice_line_ids = fields.Many2many(
        "account.move_line",
        "Invoice lines",
        relation="sale_order_line_invoice_rel",
        column1="sale_line_id",
        column2="invoice_line_id",
        copy=False,
        readonly=True,
        groups=INVOICE_LINE_GROUPS,
    )
    qty_invoiced = fields.Float("Invoiced", digits=(16, 3), compute="_compute_qty_invoiced", store=True, copy=False)
    qty_refunded = fields.Float("Credited", digits=(16, 3), compute="_compute_qty_invoiced", store=True, copy=False)
    qty_to_invoice = fields.Float(
        "To invoice", digits=(16, 3), compute="_compute_qty_to_invoice", store=True, copy=False
    )
    untaxed_amount_to_invoice = fields.Monetary(
        "Untaxed to invoice", currency_field="currency_id", compute="_compute_qty_to_invoice", store=True, copy=False
    )
    invoice_status = fields.Selection(
        INVOICE_STATUS, "Invoicing", compute="_compute_qty_to_invoice", store=True, copy=False
    )

    @api.depends("quantity", "price_unit", "discount", "tax_ids", "display_type", "currency_id")
    def _compute_amounts(self) -> None:
        for line in self:
            if line.display_type:
                line.price_subtotal = line.price_tax = line.price_total = 0.0
                continue
            res = line._tax_result(line.quantity or 0.0)
            line.price_subtotal = res["total_excluded"]
            line.price_total = res["total_included"]
            line.price_tax = res["total_included"] - res["total_excluded"]

    def _price_reduced(self) -> float:
        return float(self.price_unit or 0.0) * (1 - float(self.discount or 0.0) / 100.0)

    def _tax_result(self, quantity: float) -> dict[str, Any]:
        """``account.tax.compute_all`` of ``quantity`` units of this line (discount applied)."""
        taxes = self.elevated().tax_ids
        currency = self.elevated().currency_id or self.elevated().quotation_id.currency_id
        partner = self.elevated().quotation_id.partner_id
        return taxes.compute_all(
            self._price_reduced(), currency, quantity, self.product_id.elevated() or None, partner or None
        )

    @api.depends("product_id.invoice_policy", "product_id.service_policy", "product_id.type")
    def _compute_policies(self) -> None:
        for line in self:
            product = line.product_id.elevated()
            line.invoice_policy = (product.invoice_policy or "order") if product else "order"
            milestones = product and product.type == "service" and product.service_policy == "milestones"
            line.qty_delivered_method = "milestones" if milestones else "manual"

    @api.depends(
        "invoice_line_ids",
        "invoice_line_ids.quantity",
        "invoice_line_ids.parent_state",
        "invoice_line_ids.move_type",
        "invoice_line_ids.uom_id",
        "uom_id",
    )
    def _compute_qty_invoiced(self) -> None:
        items = self.env["account.move_line"].elevated()
        linked = items.search([("sale_line_ids", "in", self.ids)]) if self.ids else items
        by_line: dict[int, list[Model]] = defaultdict(list)
        for item in linked:
            for sale_line_id in item.sale_line_ids.ids:
                by_line[sale_line_id].append(item)
        for line in self:
            invoiced = refunded = 0.0
            for item in by_line.get(line.ids[0], []) if line.ids else []:
                if item.parent_state == "cancel" or item.move_type not in ("out_invoice", "out_refund"):
                    continue
                qty = float(item.quantity or 0.0)
                if item.uom_id and line.uom_id and item.uom_id.ids != line.uom_id.ids:
                    uom = item.uom_id.elevated()
                    if uom.category == line.uom_id.elevated().category:
                        qty = uom._compute_quantity(qty, line.uom_id.elevated(), round=False)
                if item.move_type == "out_refund":
                    refunded += qty
                    invoiced -= qty
                else:
                    invoiced += qty
            line.qty_invoiced = invoiced
            line.qty_refunded = refunded

    @api.depends(
        "quotation_id.state",
        "invoice_policy",
        "quantity",
        "qty_delivered",
        "qty_returned",
        "qty_invoiced",
        "is_optional",
        "is_downpayment",
        "display_type",
        "price_unit",
        "discount",
        "tax_ids",
    )
    def _compute_qty_to_invoice(self) -> None:
        for line in self:
            state = line.quotation_id.state
            if line.display_type or line.is_optional or state != "accepted":
                line.qty_to_invoice = 0.0
                line.untaxed_amount_to_invoice = 0.0
                line.invoice_status = "no"
                continue
            if line.is_downpayment:
                qty = -(line.qty_invoiced or 0.0)
            else:
                qty = line._invoiceable_base() - (line.qty_returned or 0.0) - (line.qty_invoiced or 0.0)
            qty = round(qty, 6)
            line.qty_to_invoice = qty
            line.untaxed_amount_to_invoice = line._tax_result(qty)["total_excluded"] if qty else 0.0
            if abs(qty) > QTY_EPS:
                line.invoice_status = "to_invoice"
            elif line.invoice_policy == "order" and (line.qty_delivered or 0.0) > (line.quantity or 0.0) + QTY_EPS:
                line.invoice_status = "upselling"
            elif (line.qty_invoiced or 0.0) + QTY_EPS >= (line.quantity or 0.0) - (line.qty_returned or 0.0):
                line.invoice_status = "invoiced"  # everything ordered (less returns) is invoiced
            else:
                line.invoice_status = "no"  # waiting for deliveries (delivery policy)

    def _invoiceable_base(self) -> float:
        """Ordered or delivered quantity, depending on the invoicing policy."""
        if self.invoice_policy == "delivery":
            return float(self.qty_delivered or 0.0)
        return float(self.quantity or 0.0)

    @api.constrains("discount", "quantity", "display_type", "qty_delivered", "qty_returned")
    def _check_values(self) -> None:
        for line in self:
            if not 0 <= (line.discount or 0) <= 100:
                raise ValidationError(_("A discount is between 0 and 100 %"), field="discount")
            if not line.display_type and (line.quantity or 0) < 0:
                raise ValidationError(_("Quantities cannot be negative"), field="quantity")
            if (line.qty_delivered or 0) < 0 or (line.qty_returned or 0) < 0:
                raise ValidationError(_("Delivered and returned quantities cannot be negative"), field="qty_returned")
            if (line.qty_returned or 0) > max(line.quantity or 0, line.qty_delivered or 0) + QTY_EPS:
                raise ValidationError(
                    _("More is returned than was ordered or delivered ({name})", name=line.name or ""),
                    field="qty_returned",
                )

    # -- create / write ------------------------------------------------------------------------------

    def _check_sellable(self, product: Model, vals: dict[str, Any]) -> None:
        if vals.get("is_downpayment") or vals.get("is_reward_line") or self.env.context.get(INTERNAL):
            return
        if not product.is_sellable:
            raise ValidationError(
                _(
                    "'{product}' cannot be sold: choose a product that can be sold (not a template)",
                    product=product.name,
                ),
                field="product_id",
            )

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = _batch(vals_list)
        internal = bool(self.env.context.get(INTERNAL))
        orders = self.env["sale.quotation"]
        products = self.env["product.product"].elevated()
        for vals in batch:
            order = orders.browse([int(vals["quotation_id"])]) if vals.get("quotation_id") else None
            if order is not None and order.elevated().locked and not internal:
                raise UserError(_("Sales order {name} is locked: unlock it to change it", name=order.elevated().name))
            if not vals.get("product_id") or vals.get("display_type"):
                continue
            product = products.browse([int(vals["product_id"])])
            self._check_sellable(product, vals)
            vals.setdefault("name", product.description_sale or product.display_name)
            if product.uom_id and not vals.get("uom_id"):
                vals["uom_id"] = product.uom_id.ids[0]
            if order is None:
                continue
            if vals.get("price_unit") is None:
                uom = self.env["product.uom"].browse([int(vals["uom_id"])]) if vals.get("uom_id") else None
                vals["price_unit"] = order._price_for(product, float(vals.get("quantity") or 1.0), uom)
                vals["price_manual"] = False
            elif "price_manual" not in vals and not internal:
                vals["price_manual"] = True
            if "tax_ids" not in vals:
                vals["tax_ids"] = [("set", order._taxes_for_product(product).ids)]
        lines = super().create(batch)
        if not self.env.context.get("sale_no_suggestions"):
            lines._add_optional_suggestions()
        return lines

    def write(self, vals: dict[str, Any]) -> bool:
        vals = dict(vals)
        internal = bool(self.env.context.get(INTERNAL))
        if not internal:
            for line in self:
                order = line.quotation_id.elevated()
                if order.locked and set(vals) - LOCKED_LINE_WRITABLE:
                    raise UserError(_("Sales order {name} is locked: unlock it to change it", name=order.name))
            if vals.get("product_id"):
                self._check_sellable(self.env["product.product"].elevated().browse([int(vals["product_id"])]), {})
            if "price_unit" in vals:
                if vals["price_unit"] is None:  # back to the pricelist price
                    del vals["price_unit"]
                    vals["price_manual"] = False
                else:
                    vals.setdefault("price_manual", True)
        result = super().write(vals)
        if internal:
            return result
        product_changed = "product_id" in vals
        if product_changed:
            for line in self.filtered(lambda r: r.product_id and not r.display_type):
                product = line.product_id.elevated()
                updates: dict[str, Any] = {}
                if "uom_id" not in vals and product.uom_id:
                    updates["uom_id"] = product.uom_id.ids[0]
                if "name" not in vals:
                    updates["name"] = product.description_sale or product.display_name
                if updates:
                    line.with_context(**{INTERNAL: True}).write(updates)
            if "tax_ids" not in vals:
                self.filtered(lambda r: r.product_id and not r.is_reward_line)._update_taxes()
        if product_changed or {"quantity", "uom_id", "price_manual"} & set(vals):
            self.filtered(
                lambda r: (
                    r.product_id
                    and not r.price_manual
                    and not r.is_downpayment
                    and not r.is_reward_line
                    and not r.display_type
                )
            )._update_price()
        return result

    def unlink(self) -> bool:
        if not self.env.context.get(INTERNAL):
            for line in self:
                order = line.quotation_id.elevated()
                if order.locked:
                    raise UserError(_("Sales order {name} is locked: unlock it to change it", name=order.name))
                if abs(line.qty_invoiced or 0.0) > QTY_EPS:
                    raise UserError(_("Invoiced line '{name}' cannot be deleted", name=line.name or ""))
        return super().unlink()

    def _update_price(self) -> None:
        """Unit price from the order's pricelist (lines whose price was not typed)."""
        for line in self:
            if not line.product_id:
                continue
            order = line.quotation_id
            price = order._price_for(line.product_id, line.quantity or 1.0, line.uom_id or None)
            if abs(price - float(line.price_unit or 0.0)) > 1e-9 or line.price_manual:
                line.with_context(**{INTERNAL: True}).write({"price_unit": price, "price_manual": False})

    def _update_taxes(self) -> None:
        """Taxes from the product, mapped by the order's fiscal position."""
        for line in self:
            if not line.product_id:
                continue
            taxes = line.quotation_id._taxes_for_product(line.product_id)
            if set(taxes.ids) != set(line.tax_ids.ids):
                line.with_context(**{INTERNAL: True}).write({"tax_ids": [("set", taxes.ids)]})

    def _add_optional_suggestions(self) -> None:
        """Offer the optional products of newly added products as optional lines (not in the totals)."""
        by_order: dict[int, list[Model]] = defaultdict(list)
        for line in self:
            if (
                line.product_id
                and not line.display_type
                and not line.is_optional
                and not line.is_reward_line
                and not line.is_downpayment
                and line.quotation_id.state in EDITABLE
            ):
                by_order[line.quotation_id.ids[0]].append(line)
        for order_id, lines in by_order.items():
            order = self.env["sale.quotation"].browse([order_id])
            present = set(order.elevated().line_ids.mapped("product_id").ids)
            to_add: list[dict[str, Any]] = []
            for line in lines:
                for option in line.product_id.elevated().optional_product_ids:
                    if option.ids[0] in present or not option.is_sellable or not option.active:
                        continue
                    present.add(option.ids[0])
                    to_add.append(
                        {
                            "quotation_id": order_id,
                            "product_id": option.ids[0],
                            "quantity": 1.0,
                            "is_optional": True,
                            "sequence": (line.sequence or 10) + 1,
                        }
                    )
            if to_add:
                self.with_context(sale_no_suggestions=True).create(to_add)
