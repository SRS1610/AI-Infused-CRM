"""Rental lines on sales orders.

A rental line (``is_rental``) rents ``quantity`` units of a rentable product from ``start_date`` to
``return_date``. Its unit price is the best price for that duration (``product.rental_price``; typed prices are
kept). Confirming the order reserves the units (``sale.rental_allocation``, refused when not enough units are free);
cancelling it or putting it back to draft releases them. Then: *pickup* (status ``pickedup``), *return* (status
``returned``, the late fee is added as an order line to invoice and the deposit is marked returned so it can be
credited), and the scheduled job marks rentals not back on time ``late`` and notifies the salesperson.

Rental lines are invoiced on the ordered quantity whatever the product's invoicing policy; the deposit is a separate
line (booked on the customer deposits account), as are late fees (rental revenue).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.sale.models.quotation import INTERNAL, QTY_EPS
from modules.sale_rental.pricing import late_fee, wall_clock_hours, zone
from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

RENTAL_STATUS = [("reserved", "Reserved"), ("pickedup", "Picked up"), ("returned", "Returned"), ("late", "Late")]
RENTAL_CHARGES = [("deposit", "Security deposit"), ("late_fee", "Late fee")]
# changes that move or resize a reservation
RESERVATION_KEYS = {"is_rental", "start_date", "return_date", "quantity", "product_id"}
OUT = ("pickedup", "late")


def _when(value: str | dt.datetime | None, env: Any) -> dt.datetime:
    if value is None or value == "":
        return env.now()
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise UserError(_("Use ISO 8601 date-times")) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _refresh_lines(orders: Model, recompute: bool = False) -> None:
    """Lines added by the rental code (deposits, late fees) while the order itself was being created or written:
    drop the order's cached line list so it and its totals include them."""
    if not orders.ids:
        return
    orders.env.cache.invalidate("sale.quotation", ["line_ids", "rental_line_ids"], orders.ids)
    if recompute:
        orders.modified(["line_ids"])
        orders.env.flush()


class QuotationLineRental(Model):
    _extend = "sale.quotation_line"

    is_rental = fields.Boolean("Rental", help="Rent the product for a period instead of selling it")
    start_date = fields.Datetime("Pickup date", help="Planned start of the rental")
    return_date = fields.Datetime("Return date", help="Planned end of the rental")
    rental_status = fields.Selection(RENTAL_STATUS, "Rental status", readonly=True, copy=False, index=True)
    pickup_datetime = fields.Datetime("Picked up at", readonly=True, copy=False)
    returned_datetime = fields.Datetime("Returned at", readonly=True, copy=False)
    late_fee = fields.Monetary("Late fee", currency_field="currency_id", readonly=True, copy=False)
    rental_price_detail = fields.Char("Rental price made of", readonly=True, copy=False)
    rental_allocation_ids = fields.One2many("sale.rental_allocation", "line_id", "Reserved units", readonly=True)
    rental_origin_line_id = fields.Many2one(
        "sale.quotation_line", "Rental", ondelete="cascade", readonly=True, copy=False, index=True
    )
    rental_charge = fields.Selection(RENTAL_CHARGES, "Rental charge", readonly=True)

    # -- helpers -------------------------------------------------------------------------------------

    def _rental_tz(self) -> str:
        """Time zone rental durations are measured in: the salesperson's, else the current user's."""
        order = self.elevated().quotation_id
        return (order.user_id.tz if order and order.user_id else None) or str(self.env.tz)

    def _auto_name(self, product: Model, start: Any, end: Any, tz: str) -> str:
        product = product.elevated()
        base = product.description_sale or product.display_name
        if not (start and end):
            return base
        z = zone(tz)
        s = _when(start, self.env).astimezone(z).strftime("%Y-%m-%d %H:%M")
        e = _when(end, self.env).astimezone(z).strftime("%Y-%m-%d %H:%M")
        return f"{base}\n{_('Rental from {start} to {end}', start=s, end=e)}"

    def _charges(self, kind: str) -> Model:
        if not self.ids:
            return self.browse([])
        return (
            self.elevated()
            .search([("rental_origin_line_id", "in", self.ids), ("rental_charge", "=", kind)])
            .with_env(self.env)
        )

    def _check_sellable(self, product: Model, vals: dict[str, Any]) -> None:
        if vals.get("rental_charge"):  # deposit and late fee products are not sold on their own
            return
        super()._check_sellable(product, vals)

    # -- constraints ---------------------------------------------------------------------------------

    @api.constrains("is_rental", "product_id", "start_date", "return_date", "quantity")
    def _check_rental(self) -> None:
        for line in self:
            if not line.is_rental or line.display_type:
                continue
            product = line.product_id.elevated()
            if not product or not product.rent_ok:
                raise ValidationError(
                    _("'{product}' cannot be rented", product=product.display_name if product else ""),
                    field="product_id",
                )
            if line.start_date and line.return_date and line.return_date <= line.start_date:
                raise ValidationError(_("The return date must be after the pickup date"), field="return_date")
            qty = float(line.quantity or 0.0)
            if abs(qty - round(qty)) > QTY_EPS:
                raise ValidationError(_("Rentals are by whole units"), field="quantity")

    # -- create / write ------------------------------------------------------------------------------

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        products = self.env["product.product"].elevated()
        orders = self.env["sale.quotation"].elevated()
        for vals in batch:
            if vals.get("display_type") or not vals.get("product_id"):
                continue
            product = products.browse([int(vals["product_id"])])
            if "is_rental" not in vals and product.rent_ok and (vals.get("start_date") or vals.get("return_date")):
                vals["is_rental"] = True
            if vals.get("is_rental") and not vals.get("name") and vals.get("quotation_id"):
                order = orders.browse([int(vals["quotation_id"])])
                tz = (order.user_id.tz if order.user_id else None) or str(self.env.tz)
                vals["name"] = self._auto_name(product, vals.get("start_date"), vals.get("return_date"), tz)
        if not any(v.get("is_rental") for v in batch):
            return super().create(batch)
        with self.env.cr.savepoint():  # a refused reservation undoes the new lines
            lines = super().create(batch)
            rental = lines.filtered(lambda r: r.is_rental and not r.display_type)
            rental.filtered(lambda r: not r.price_manual)._update_price()
            rental._sync_deposit()
            rental.filtered(lambda r: r.quotation_id.state == "accepted")._reserve()
        return lines

    def write(self, vals: dict[str, Any]) -> bool:
        if self.env.context.get(INTERNAL):
            return super().write(vals)
        keys = RESERVATION_KEYS & set(vals)
        if keys:
            for line in self:
                if line.rental_status == "returned":
                    raise UserError(_("Rental '{name}' is returned: it can no longer change", name=line.name or ""))
                if line.rental_status in OUT and keys - {"return_date"}:
                    raise UserError(
                        _("Rental '{name}' is picked up: only its return date can change", name=line.name or "")
                    )
        renamed: dict[int, str] = {}
        if keys & {"start_date", "return_date", "product_id", "is_rental"} and "name" not in vals:
            for line in self.filtered(lambda r: r.is_rental and r.product_id):
                auto = line._auto_name(line.product_id, line.start_date, line.return_date, line._rental_tz())
                if (line.name or "") == auto:
                    renamed[line.ids[0]] = auto
        if not keys:
            return super().write(vals)
        # a refused reservation undoes the change
        with self.env.cr.savepoint():
            result = super().write(vals)
            self._after_rental_change(renamed)
        return result

    def _after_rental_change(self, renamed: dict[int, str]) -> None:
        for line in self.filtered(lambda r: r.ids[0] in renamed):
            line.with_context(**{INTERNAL: True}).write(
                {"name": line._auto_name(line.product_id, line.start_date, line.return_date, line._rental_tz())}
            )
        rental = self.filtered(lambda r: r.is_rental and not r.display_type)
        rental.filtered(lambda r: not r.price_manual)._update_price()
        (self - rental)._release()
        (self - rental)._charges("deposit").filtered(lambda r: abs(r.qty_invoiced or 0.0) <= QTY_EPS).with_context(
            **{INTERNAL: True}
        ).unlink()
        rental._sync_deposit()
        rental.filtered(lambda r: r.quotation_id.state == "accepted")._reserve()

    def unlink(self) -> bool:
        charges = (self._charges("deposit") | self._charges("late_fee")) - self
        if charges:
            charges.unlink()
        return super().unlink()

    # -- pricing and deposit -------------------------------------------------------------------------

    def _update_price(self) -> None:
        rental = self.filtered(lambda r: r.is_rental and r.product_id and r.start_date and r.return_date)
        super(QuotationLineRental, self - rental)._update_price()
        for line in rental:
            price, detail = line.product_id.rental_price(
                line.quotation_id, line.start_date, line.return_date, line._rental_tz()
            )
            updates: dict[str, Any] = {}
            if abs(price - float(line.price_unit or 0.0)) > 1e-9 or line.price_manual:
                updates.update({"price_unit": price, "price_manual": False})
            if detail != (line.rental_price_detail or ""):
                updates["rental_price_detail"] = detail
            if updates:
                line.with_context(**{INTERNAL: True}).write(updates)

    def _deposit_amount(self) -> float:
        product = self.product_id.elevated()
        order = self.quotation_id.elevated()
        amount = float(product.rental_deposit or 0.0)
        if amount and product.currency_id and product.currency_id.ids != order.currency_id.ids:
            day = order.date_order.date() if order.date_order else self.env.today()
            amount = product.currency_id.convert(amount, order.currency_id, order.company_id, day)
        return float(order.currency_id.round(amount)) if amount else 0.0

    def _sync_deposit(self) -> None:
        """One *security deposit* line per rental line whose product asks for a deposit (quantity = units)."""
        product = self.env.ref("sale_rental.product_rental_deposit", raise_if_not_found=False)
        lines = self.env["sale.quotation_line"].with_context(**{INTERNAL: True, "sale_no_suggestions": True})
        for line in self:
            deposit = line._charges("deposit")
            amount = line._deposit_amount()
            frozen = deposit.filtered(lambda r: abs(r.qty_invoiced or 0.0) > QTY_EPS)
            if not amount or product is None:
                (deposit - frozen).with_context(**{INTERNAL: True}).unlink()
                continue
            vals = {
                "name": str(_("Security deposit: {product}", product=line.product_id.elevated().display_name)),
                "quantity": float(line.quantity or 0.0),
                "price_unit": amount,
            }
            if deposit:
                if not frozen and (
                    abs(deposit.price_unit - amount) > 1e-9 or abs(deposit.quantity - vals["quantity"]) > QTY_EPS
                ):
                    deposit.with_context(**{INTERNAL: True}).write(vals)
                continue
            lines.create(
                {
                    **vals,
                    "quotation_id": line.quotation_id.ids[0],
                    "product_id": product.ids[0],
                    "price_manual": True,
                    "tax_ids": [("set", [])],
                    "rental_origin_line_id": line.ids[0],
                    "rental_charge": "deposit",
                    "sequence": (line.sequence or 10) + 1,
                }
            )

    # -- reservations --------------------------------------------------------------------------------

    def _reserve(self) -> None:
        """Reserve the units of these rental lines of confirmed orders (picked-up rentals keep their units and only
        move their end)."""
        allocations = self.env["sale.rental_allocation"].elevated("rental reservation")
        for line in self:
            if not line.is_rental or line.display_type or line.rental_status == "returned":
                continue
            if not (line.start_date and line.return_date):
                raise UserError(
                    _("Set the pickup and return dates of '{name}' before confirming", name=line.name or "")
                )
            held = line.elevated().rental_allocation_ids
            if line.rental_status in OUT:
                try:
                    with self.env.cr.savepoint():
                        held.write({"date_end": line.return_date})
                        self.env.flush()
                except ValidationError as exc:
                    raise UserError(
                        _(
                            "The units of '{name}' are booked by another rental before the new return date",
                            name=line.name or "",
                        )
                    ) from exc
                continue
            held.unlink()
            allocations.allocate(line, line.start_date, line.return_date)
            if line.rental_status != "reserved":
                line.with_context(**{INTERNAL: True}).write({"rental_status": "reserved"})

    def _release(self) -> None:
        """Free the units (order cancelled, back to draft, line no longer a rental)."""
        held = self.elevated().mapped("rental_allocation_ids")
        if held:
            held.unlink()
        reserved = self.filtered(lambda r: r.rental_status)
        if reserved:
            reserved.with_context(**{INTERNAL: True}).write({"rental_status": None})

    # -- pickup and return ---------------------------------------------------------------------------

    def _check_rental_action(self) -> None:
        self.check_access("write")
        for line in self:
            if not line.is_rental:
                raise UserError(_("'{name}' is not a rental", name=line.name or ""))
            if line.quotation_id.state != "accepted":
                raise UserError(_("Confirm the order first ({name})", name=line.quotation_id.name))

    @api.expose
    def action_pickup(self, when: str | None = None) -> bool:
        """The customer takes the units (``when``: ISO date-time, default now)."""
        self._check_rental_action()
        moment = _when(when, self.env)
        for line in self:
            if line.rental_status != "reserved":
                raise UserError(_("'{name}' is not waiting for pickup", name=line.name or ""))
            line.with_context(**{INTERNAL: True}).write(
                {"rental_status": "pickedup", "pickup_datetime": moment, "qty_delivered": line.quantity}
            )
            line.quotation_id._rental_log(_("Picked up: {name}", name=line.name or ""))
        return True

    @api.expose
    def action_return(self, when: str | None = None) -> bool:
        """The customer brings the units back (``when``: ISO date-time, default now): late fee, deposit, units
        released from that moment."""
        self._check_rental_action()
        moment = _when(when, self.env)
        for line in self:
            if line.rental_status not in OUT:
                raise UserError(_("'{name}' is not out with the customer", name=line.name or ""))
            if line.pickup_datetime and moment < line.pickup_datetime:
                raise UserError(_("The return is before the pickup"))
            fee, hours = line._late_fee_at(moment)
            line.with_context(**{INTERNAL: True}).write(
                {"rental_status": "returned", "returned_datetime": moment, "late_fee": fee}
            )
            for held in line.elevated().rental_allocation_ids:
                if moment <= held.date_start:
                    held.unlink()
                elif moment < held.date_end:
                    held.write({"date_end": moment})
            if fee:
                line._add_late_fee(fee, hours)
            deposit = line._charges("deposit")
            if deposit:
                deposit.with_context(**{INTERNAL: True}).write({"qty_returned": deposit.quantity})
            line.quotation_id._rental_log(_("Returned: {name}", name=line.name or ""))
        return True

    def _late_fee_at(self, moment: dt.datetime) -> tuple[float, int]:
        """(late fee for the line's units, started hours late) for a return at ``moment``."""
        if not self.return_date or moment <= self.return_date:
            return 0.0, 0
        hours = wall_clock_hours(self.return_date, moment, self._rental_tz())
        product = self.product_id.elevated()
        order = self.quotation_id.elevated()
        fee = late_fee(hours, float(product.extra_hourly or 0.0), float(product.extra_daily or 0.0))
        fee *= float(self.quantity or 0.0)
        if fee and product.currency_id and product.currency_id.ids != order.currency_id.ids:
            fee = product.currency_id.convert(fee, order.currency_id, order.company_id, moment.date())
        return float(order.currency_id.round(fee)), hours

    def _add_late_fee(self, fee: float, hours: int) -> None:
        product = self.env.ref("sale_rental.product_rental_late_fee", raise_if_not_found=False)
        order = self.quotation_id
        if product is None:
            raise UserError(_("The late fee product is missing: upgrade the rental module"))
        self.env["sale.quotation_line"].with_context(**{INTERNAL: True, "sale_no_suggestions": True}).create(
            {
                "quotation_id": order.ids[0],
                "product_id": product.ids[0],
                "name": str(
                    _("Late return: {product} ({hours} h late)", product=self.product_id.display_name, hours=hours)
                ),
                "quantity": 1.0,
                "price_unit": fee,
                "price_manual": True,
                "tax_ids": [("set", self.tax_ids.ids)],
                "rental_origin_line_id": self.ids[0],
                "rental_charge": "late_fee",
                "sequence": (self.sequence or 10) + 2,
            }
        )
        if order.elevated().locked:  # the fee is still to invoice
            order.with_context(**{INTERNAL: True}).write({"locked": False})


class QuotationRental(Model):
    _extend = "sale.quotation"

    is_rental_order = fields.Boolean("Rental order", compute="_compute_rental", store=True, index=True)
    rental_status = fields.Selection(RENTAL_STATUS, "Rental status", compute="_compute_rental", store=True, index=True)
    rental_line_ids = fields.One2many(
        "sale.quotation_line", "quotation_id", "Rentals", domain=[["is_rental", "=", True]], copy=False
    )

    @api.depends("line_ids.is_rental", "line_ids.rental_status", "line_ids.display_type")
    def _compute_rental(self) -> None:
        for order in self:
            lines = order.line_ids.filtered(lambda r: r.is_rental and not r.display_type)
            statuses = {line.rental_status for line in lines}
            order.is_rental_order = bool(lines)
            if "late" in statuses:
                order.rental_status = "late"
            elif "pickedup" in statuses:
                order.rental_status = "pickedup"
            elif "reserved" in statuses:
                order.rental_status = "reserved"
            elif statuses == {"returned"}:
                order.rental_status = "returned"
            else:
                order.rental_status = None

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        records = super().create(vals_list)
        _refresh_lines(records, recompute=True)  # deposits were added while the lines were created
        return records

    def _rental_lines(self) -> Model:
        return self.mapped("line_ids").filtered(lambda r: r.is_rental and not r.display_type)

    def write(self, vals: dict[str, Any]) -> bool:
        if "line_ids" in vals and "state" not in vals:
            result = super().write(vals)
            _refresh_lines(self, recompute=True)  # deposits added with new rental lines
            return result
        target = vals.get("state")
        if target == "accepted":
            confirming = self.filtered(lambda q: q.state != "accepted")
            if confirming._rental_lines():
                # the reservations disappear with the confirmation if anything fails
                with self.env.cr.savepoint():
                    confirming._rental_lines()._reserve()
                    return super().write(vals)
        elif target:
            leaving = self.filtered(lambda q: q.state == "accepted")
            for line in leaving._rental_lines():
                if line.rental_status in OUT:
                    raise UserError(
                        _("'{name}' is still with the customer: register its return first", name=line.name or "")
                    )
            result = super().write(vals)
            leaving._rental_lines()._release()
            return result
        return super().write(vals)

    def copy(self, default: dict[str, Any] | None = None) -> Model:
        new = super().copy(default)
        # deposit and late fee lines follow their rental line (the copied rental lines made new deposits)
        stale = self.env["sale.quotation_line"].search(
            [("quotation_id", "=", new.ids[0]), ("rental_charge", "!=", None), ("rental_origin_line_id", "=", None)]
        )
        if stale:
            stale.with_context(**{INTERNAL: True}).unlink()
            _refresh_lines(new, recompute=True)
        return new

    def _rental_log(self, text: Any) -> None:
        from modules.chatter.thread import post_message

        post_message(self.env, self._name, self.ids[0], f"<p>{text}</p>", message_type="notification", notify=False)

    @api.expose
    def action_rental_pickup(self) -> bool:
        """Pick up every reserved rental of these orders."""
        lines = self._rental_lines().filtered(lambda r: r.rental_status == "reserved")
        if not lines:
            raise UserError(_("Nothing is waiting for pickup"))
        return lines.action_pickup()

    @api.expose
    def action_rental_return(self) -> bool:
        """Return every rental of these orders that is out with the customer."""
        lines = self._rental_lines().filtered(lambda r: r.rental_status in OUT)
        if not lines:
            raise UserError(_("Nothing is out with the customer"))
        return lines.action_return()

    @api.expose
    def action_create_invoice(self, method: str = "delivered", amount: float | None = None) -> dict[str, Any]:
        result = super().action_create_invoice(method, amount)
        _rental_accounts(self.env["account.move"].browse(result.get("invoice_ids") or []))
        return result

    @api.expose
    def action_create_credit_note(self) -> dict[str, Any]:
        result = super().action_create_credit_note()
        _rental_accounts(self.env["account.move"].browse(result.get("invoice_ids") or []))
        return result


class LinePolicyRental(Model):
    _extend = "sale.quotation_line"

    @api.depends("is_rental", "rental_charge")
    def _compute_policies(self) -> None:
        super()._compute_policies()
        for line in self:
            if line.is_rental or line.rental_charge:
                line.invoice_policy = "order"  # rentals are invoiced by order, whatever the product's policy


def _rental_accounts(moves: Model) -> None:
    """Book rental lines and late fees on rental revenue (unless the product has its own income account in the
    company) and deposits on customer deposits, on draft invoices made from orders."""
    from modules.account.chart import chart_ref

    for move in moves.elevated():
        if move.state != "draft":
            continue
        company = move.company_id
        rental = chart_ref(company, "income_rental")
        deposit = chart_ref(company, "customer_deposits")
        for item in move.invoice_line_ids:
            sources = item.sale_line_ids
            if not sources:
                continue
            target = None
            if any(s.rental_charge == "deposit" for s in sources):
                target = deposit
            elif any(s.is_rental or s.rental_charge == "late_fee" for s in sources):
                own = item.product_id.property_account_income_id if item.product_id else None
                if not (own and own.company_id.ids == company.ids):
                    target = rental
            if target is None:
                continue
            if move.fiscal_position_id:
                target = move.fiscal_position_id.map_account(target)
            if item.account_id.ids != target.ids:
                item.write({"account_id": target.ids[0]})
