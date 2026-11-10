"""Invoices from sales orders: regular invoices, down payments and credit notes for returns.

* Regular invoices (``create_invoices``) take every line's ``qty_to_invoice`` when it is positive (ordered or
  delivered quantity, depending on the product's invoicing policy, minus returns and what is already
  invoiced) and deduct the down payments invoiced earlier with negative lines. Orders of the same customer,
  currency and company share one invoice; each order starts a section when there are several.
* Down payments (``create_downpayment_invoices``) are a percentage of the order total or a fixed,
  tax-included amount. They are recorded on the order as ``is_downpayment`` lines (quantity 0, one per set of
  taxes of the order lines, so the deposit carries the same taxes) and invoiced with the *Down payment*
  service product on the company's customer deposits account.
* Credit notes for returns (``create_return_credit_notes``) credit the returned quantities that were already
  invoiced (lines whose quantity to invoice is negative).

Invoices are created as drafts with the order's fiscal position, payment terms and currency (the pricelist's)
and ``invoice_origin`` = the order numbers; their lines are linked to the order lines (``sale_line_ids``).
The caller needs the billing rights (``account.group_account_invoice``).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from modules.sale.models.quotation import INTERNAL, QTY_EPS
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model


def downpayment_product(env: Any) -> Model:
    product = env.ref("sale.product_downpayment", raise_if_not_found=False)
    if product is None:
        raise UserError(_("The down payment product is missing: upgrade the sale module"))
    return env["product.product"].browse(product.ids)


def deposit_account(company: Model) -> Model | None:
    """The account down payments are booked on: customer deposits, else the product's or the default income."""
    from modules.account.chart import chart_ref

    account = chart_ref(company, "customer_deposits")
    if account:
        return account
    product = downpayment_product(company.env).elevated()
    income = product.property_account_income_id
    if income and income.company_id.ids == company.ids:
        return income
    return company.elevated().default_income_account_id or None


def _check_orders(orders: Model) -> None:
    for order in orders:
        order.check_access("write")
        if order.state != "accepted":
            raise UserError(
                _("Only sales orders can be invoiced ({name}): confirm the quotation first", name=order.name)
            )


def _move_values(orders: Model, move_type: str = "out_invoice") -> dict[str, Any]:
    first = orders[0]
    names = ", ".join(orders.mapped("name"))
    refs = ", ".join(r for r in orders.mapped("client_reference") if r)
    vals: dict[str, Any] = {
        "move_type": move_type,
        "partner_id": first.partner_id.ids[0],
        "company_id": first.company_id.ids[0],
        "currency_id": first.currency_id.ids[0],
        "invoice_origin": names,
        "fiscal_position_id": first.fiscal_position_id.ids[0] if first.fiscal_position_id else None,
        "payment_term_id": first.payment_term_id.ids[0] if first.payment_term_id else None,
        "invoice_user_id": first.user_id.ids[0] if first.user_id else None,
    }
    if refs:
        vals["ref"] = refs
    if move_type == "out_refund":
        vals["payment_term_id"] = None
    return vals


def _line_values(line: Model, quantity: float, sequence: int) -> dict[str, Any]:
    vals: dict[str, Any] = {
        "display_type": "product",
        "sequence": sequence,
        "name": line.name,
        "product_id": line.product_id.ids[0] if line.product_id else None,
        "uom_id": line.uom_id.ids[0] if line.uom_id else None,
        "quantity": quantity,
        "price_unit": line.price_unit,
        "discount": line.discount or 0.0,
        "tax_ids": [("set", line.tax_ids.ids)],
        "sale_line_ids": [("set", line.ids)],
    }
    if line.is_downpayment:
        deposit = _downpayment_account(line)
        if deposit is not None:
            vals["account_id"] = deposit.ids[0]
    return vals


def _downpayment_account(line: Model) -> Model | None:
    """Account of a down payment's invoice: the one used when it was invoiced, else the deposits account."""
    items = line.env["account.move_line"].elevated().search([("sale_line_ids", "in", line.ids)], limit=1)
    if items and items.account_id:
        return line.env["account.account"].browse(items.account_id.ids)
    return deposit_account(line.quotation_id.company_id.elevated())


def _group_key(order: Model) -> tuple[int, int, int]:
    return (order.company_id.ids[0], order.partner_id.ids[0], order.currency_id.ids[0])


def _create_moves(
    env: Any, groups: dict[tuple[Any, ...], list[tuple[Model, list[dict[str, Any]]]]], move_type: str
) -> Model:
    moves = env["account.move"]
    for entries in groups.values():
        orders = env["sale.quotation"].browse([o.ids[0] for o, _lines in entries])
        commands: list[Any] = []
        sequence = 10
        for order, values in entries:
            if len(entries) > 1:
                commands.append(("create", {"display_type": "line_section", "name": order.name, "sequence": sequence}))
                sequence += 1
            for vals in values:
                vals["sequence"] = sequence
                sequence += 1
                commands.append(("create", vals))
        move = (
            env["account.move"]
            .with_company(orders[0].company_id)
            .create({**_move_values(orders, move_type), "invoice_line_ids": commands})
        )
        moves |= move
    return moves


def create_invoices(orders: Model) -> Model:
    """Invoice what is invoiceable on ``orders`` (one invoice per customer, currency and company)."""
    _check_orders(orders)
    groups: dict[tuple[Any, ...], list[tuple[Model, list[dict[str, Any]]]]] = defaultdict(list)
    for order in orders.sorted(lambda o: o.ids[0]):
        lines = order.line_ids.filtered(lambda line: not line.display_type and not line.is_optional)
        regular = [
            _line_values(line, line.qty_to_invoice, 0)
            for line in lines
            if not line.is_downpayment and line.qty_to_invoice > QTY_EPS
        ]
        if not regular:
            continue
        deductions = [
            {**_line_values(line, line.qty_to_invoice, 0), "name": _("Down payment deduction: {name}", name=line.name)}
            for line in lines
            if line.is_downpayment and line.qty_to_invoice < -QTY_EPS
        ]
        groups[_group_key(order)].append((order, regular + deductions))
    if not groups:
        raise UserError(_("There is nothing to invoice: check the delivered quantities and the invoicing policies"))
    return _create_moves(orders.env, groups, "out_invoice")


def create_downpayment_invoices(orders: Model, method: str, amount: float) -> Model:
    """A down payment invoice per order: ``amount`` % of its total (``percentage``) or ``amount`` tax included
    (``fixed``)."""
    _check_orders(orders)
    if amount <= 0 or (method == "percentage" and amount > 100):
        raise UserError(_("A down payment is a positive amount, and at most 100 % of the order"))
    product = downpayment_product(orders.env)
    moves = orders.env["account.move"]
    date = orders.env.today()
    for order in orders:
        total = float(order.amount_total or 0.0)
        if total <= 0:
            raise UserError(_("Order {name} has no amount to pay in advance", name=order.name))
        if method == "fixed" and amount > total + 0.005:
            raise UserError(_("The down payment is larger than the order total ({total})", total=f"{total:,.2f}"))
        ratio = amount / 100.0 if method == "percentage" else amount / total
        label = (
            _("Down payment of {date}: {percent} %", date=date.isoformat(), percent=f"{amount:g}")
            if method == "percentage"
            else _("Down payment of {date}", date=date.isoformat())
        )
        lines = order.line_ids.filtered(
            lambda line: (
                not line.display_type and not line.is_optional and not line.is_downpayment and (line.quantity or 0) > 0
            )
        )
        groups: dict[tuple[int, ...], float] = defaultdict(float)
        for line in lines:
            groups[tuple(sorted(line.tax_ids.ids))] += float(line.quantity or 0.0) * line._price_reduced()
        values = []
        for key, basis in groups.items():
            price = float(order.currency_id.round(basis * ratio))
            if not price:
                continue
            values.append(
                {
                    "quotation_id": order.ids[0],
                    "name": label,
                    "product_id": product.ids[0],
                    "quantity": 0.0,
                    "price_unit": price,
                    "price_manual": True,
                    "tax_ids": [("set", list(key))],
                    "is_downpayment": True,
                    "sequence": 9500,
                }
            )
        if not values:
            raise UserError(_("Order {name} has no amount to pay in advance", name=order.name))
        dp_lines = (
            order.env["sale.quotation_line"]
            .with_context(**{INTERNAL: True, "sale_no_suggestions": True})
            .create(values)
        )
        deposit = deposit_account(order.company_id.elevated())
        invoice_lines = []
        for i, line in enumerate(dp_lines):
            vals = _line_values(line, 1.0, 10 + i)
            if deposit is not None:
                vals["account_id"] = deposit.ids[0]
            invoice_lines.append(vals)
        moves |= _create_moves(order.env, {_group_key(order): [(order, invoice_lines)]}, "out_invoice")
    return moves


def create_return_credit_notes(orders: Model) -> Model:
    """Credit notes for the returned quantities already invoiced (one per order)."""
    _check_orders(orders)
    groups: dict[tuple[Any, ...], list[tuple[Model, list[dict[str, Any]]]]] = {}
    for order in orders:
        values = [
            _line_values(line, min(-line.qty_to_invoice, float(line.qty_returned or 0.0)), 0)
            for line in order.line_ids
            if not line.display_type
            and not line.is_downpayment
            and (line.qty_returned or 0.0) > QTY_EPS
            and line.qty_to_invoice < -QTY_EPS
        ]
        if values:
            groups[(order.ids[0],)] = [(order, values)]
    if not groups:
        raise UserError(_("No returned quantity is waiting for a credit note"))
    return _create_moves(orders.env, groups, "out_refund")
