"""Invoicing sales orders: policies, invoiced quantities, invoice status, grouping, down payments, credit notes
for returns, reversals and the locking of fully invoiced orders."""

from __future__ import annotations

from typing import Any

import pytest

from modules.account.chart import chart_ref
from modules.sale.tests.common import customer, no_promotions, order, product, product_lines, sale_tax
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db


@pytest.fixture
def goods(env: Any) -> dict[str, Any]:
    no_promotions(env)
    t20 = sale_tax(env, 20)
    service = product(env, "Inv service", price=100, taxes=t20, type="service", invoice_policy="order")
    item = product(env, "Inv goods", price=50, taxes=t20, invoice_policy="delivery")
    return {"t20": t20, "service": service, "goods": item, "buyer": customer(env)}


def _moves(env: Any, result: dict[str, Any]) -> Any:
    return env["account.move"].browse(result["invoice_ids"])


def test_ordered_quantities_are_invoiced(env: Any, goods: dict[str, Any]) -> None:
    term = env.ref("account.term_30")
    q = order(env, goods["buyer"], [(goods["service"], 3)], payment_term_id=term.ids[0], client_reference="PO-77")
    with pytest.raises(UserError, match="confirm the quotation first"):
        q.action_create_invoice()
    q.action_confirm()
    line = product_lines(q)
    assert (line.qty_to_invoice, line.untaxed_amount_to_invoice, q.amount_to_invoice) == (3, 300, 300)
    assert q.invoice_status == "to_invoice"
    move = _moves(env, q.action_create_invoice())
    assert move.state == "draft" and move.move_type == "out_invoice"
    assert move.invoice_origin == q.name and move.ref == "PO-77" and move.payment_term_id == term
    assert move.currency_id == q.currency_id and move.partner_id == goods["buyer"]
    assert move.amount_untaxed == 300 and move.amount_total == 360
    item = move.invoice_line_ids.filtered(lambda r: r.product_id)
    assert item.sale_line_ids == line and line.invoice_line_ids == item
    assert line.qty_invoiced == 3 and line.qty_to_invoice == 0  # drafts count
    assert q.invoice_status == "invoiced" and q.invoice_count == 1 and q.invoice_ids == move
    assert move.sale_order_count == 1
    assert not q.locked
    move.action_post()
    assert q.locked  # fully invoiced
    with pytest.raises(UserError, match="nothing to invoice"):
        q.with_context().action_create_invoice()


def test_delivered_quantities_and_cancelled_invoices(env: Any, goods: dict[str, Any]) -> None:
    q = order(env, goods["buyer"], [(goods["goods"], 4)])
    q.action_confirm()
    line = product_lines(q)
    assert line.invoice_policy == "delivery" and line.qty_to_invoice == 0 and q.invoice_status == "no"
    with pytest.raises(UserError, match="nothing to invoice"):
        q.action_create_invoice()
    line.write({"qty_delivered": 1})
    assert q.invoice_status == "to_invoice"
    first = _moves(env, q.action_create_invoice())
    assert first.invoice_line_ids.quantity == 1 and q.invoice_status == "no"
    line.write({"qty_delivered": 4})
    second = _moves(env, q.action_create_invoice())
    assert second.invoice_line_ids.quantity == 3 and line.qty_invoiced == 4 and q.invoice_status == "invoiced"
    second.button_cancel()
    assert line.qty_invoiced == 1 and line.qty_to_invoice == 3 and q.invoice_status == "to_invoice"
    first.unlink()  # a deleted draft no longer counts
    assert line.qty_invoiced == 0 and line.qty_to_invoice == 4


def test_upselling_when_more_is_delivered_than_ordered(env: Any, goods: dict[str, Any]) -> None:
    q = order(env, goods["buyer"], [(goods["service"], 2)])
    q.action_confirm()
    q.action_create_invoice()
    product_lines(q).write({"qty_delivered": 3})
    assert q.invoice_status == "upselling"


def test_several_orders_on_one_invoice(env: Any, goods: dict[str, Any]) -> None:
    first = order(env, goods["buyer"], [(goods["service"], 1)])
    second = order(env, goods["buyer"], [(goods["service"], 2)])
    other = order(env, customer(env, "Someone else"), [(goods["service"], 5)])
    for q in (first, second, other):
        q.action_confirm()
    result = env["sale.quotation"].action_invoice_orders((first | second | other).ids)
    moves = _moves(env, result)
    assert len(moves) == 2
    shared = moves.filtered(lambda m: m.partner_id == goods["buyer"])
    assert shared.invoice_origin == f"{first.name}, {second.name}"
    sections = shared.invoice_line_ids.filtered(lambda r: r.display_type == "line_section").mapped("name")
    assert sections == [first.name, second.name] and shared.amount_untaxed == 300


def test_down_payment_then_final_invoice(env: Any, goods: dict[str, Any]) -> None:
    q = order(env, goods["buyer"], [(goods["service"], 10)])
    q.action_confirm()
    with pytest.raises(UserError):
        q.action_create_invoice("percentage")  # no amount
    with pytest.raises(UserError):
        q.action_create_invoice("percentage", 150)
    with pytest.raises(UserError, match="Unknown invoicing method"):
        q.action_create_invoice("everything")
    deposit = _moves(env, q.action_create_invoice("percentage", 30))
    deposit.action_post()
    dp_line = q.line_ids.filtered(lambda r: r.is_downpayment)
    assert dp_line.quantity == 0 and dp_line.price_unit == 300 and dp_line.tax_ids == goods["t20"]
    assert q.amount_total == 1200  # the deposit is not added to the order
    item = deposit.invoice_line_ids
    assert item.product_id == env.ref("sale.product_downpayment") and item.quantity == 1
    assert item.account_id == chart_ref(q.company_id, "customer_deposits")
    assert deposit.amount_total == 360
    assert dp_line.qty_invoiced == 1 and dp_line.qty_to_invoice == -1 and q.invoice_status == "to_invoice"
    final = _moves(env, q.action_create_invoice())
    deduction = final.invoice_line_ids.filtered(lambda r: r.quantity < 0)
    assert deduction.price_unit == 300 and deduction.account_id == item.account_id
    assert final.amount_untaxed == 700 and final.amount_total == 840
    assert dp_line.qty_invoiced == 0 and q.invoice_status == "invoiced"
    final.action_post()
    assert q.locked


def test_fixed_down_payment_keeps_each_tax(env: Any, goods: dict[str, Any]) -> None:
    t10 = sale_tax(env, 10)
    other = product(env, "Inv reduced", price=100, taxes=t10, invoice_policy="order")
    q = order(env, goods["buyer"], [(goods["service"], 1), (other, 1)])  # 120 + 110 = 230 tax included
    q.action_confirm()
    with pytest.raises(UserError, match="larger than the order total"):
        q.action_create_invoice("fixed", 500)
    move = _moves(env, q.action_create_invoice("fixed", 115))  # half the order
    by_tax = {line.tax_ids.ids[0]: line.price_unit for line in move.invoice_line_ids}
    assert by_tax == {goods["t20"].ids[0]: 50.0, t10.ids[0]: 50.0} and move.amount_total == 115


def test_credit_note_for_returns(env: Any, goods: dict[str, Any]) -> None:
    q = order(env, goods["buyer"], [(goods["service"], 5)])
    q.action_confirm()
    _moves(env, q.action_create_invoice()).action_post()
    assert q.locked
    with pytest.raises(UserError, match="No returned quantity"):
        q.action_create_credit_note()
    line = product_lines(q)
    line.write({"qty_returned": 2})
    assert line.qty_to_invoice == -2 and q.invoice_status == "to_invoice"
    refund = _moves(env, q.action_create_credit_note())
    assert refund.move_type == "out_refund" and refund.invoice_line_ids.quantity == 2
    assert refund.invoice_line_ids.sale_line_ids == line and refund.amount_total == 240
    assert line.qty_invoiced == 3 and line.qty_refunded == 2 and line.qty_to_invoice == 0
    assert q.invoice_status == "invoiced"
    refund.action_post()


def test_returns_before_invoicing_reduce_what_is_invoiced(env: Any, goods: dict[str, Any]) -> None:
    q = order(env, goods["buyer"], [(goods["goods"], 3)])
    q.action_confirm()
    product_lines(q).write({"qty_delivered": 3, "qty_returned": 1})
    move = _moves(env, q.action_create_invoice())
    assert move.invoice_line_ids.quantity == 2 and q.invoice_status == "invoiced"


def test_reversal_keeps_the_links(env: Any, goods: dict[str, Any]) -> None:
    q = order(env, goods["buyer"], [(goods["service"], 2)])
    q.action_confirm()
    move = _moves(env, q.action_create_invoice())
    move.action_post()
    line = product_lines(q)
    reversal = env["account.move"].browse(move.action_reverse(reason="Wrong customer", mode="cancel"))
    assert reversal.state == "posted" and reversal.invoice_line_ids.sale_line_ids == line
    assert line.qty_invoiced == 0 and line.qty_to_invoice == 2 and q.invoice_status == "to_invoice"
    q.action_unlock()
    again = _moves(env, q.action_create_invoice())
    assert again.amount_untaxed == 200


def test_invoiced_lines_cannot_be_removed_and_orders_not_cancelled(env: Any, goods: dict[str, Any]) -> None:
    q = order(env, goods["buyer"], [(goods["service"], 1)])
    q.action_confirm()
    q.action_create_invoice()
    with pytest.raises(UserError, match="cannot be deleted"):
        product_lines(q).unlink()
    with pytest.raises(UserError, match="has invoices"):
        q.action_cancel()


def test_invoicing_needs_billing_rights(env: Any, goods: dict[str, Any]) -> None:
    def user(login: str, groups: list[str]) -> Any:
        return env["core.user"].create(
            {"name": login, "login": login, "group_ids": [("set", [env.ref(g).ids[0] for g in groups])]}
        )

    seller = env.with_user(user("seller@sale.test", ["sale.group_sale_user"]).ids[0])
    q = order(seller, seller["contacts.partner"].browse(goods["buyer"].ids), [(goods["service"], 1)])
    q.action_confirm()
    with pytest.raises(AccessError):
        q.action_create_invoice()
    assert "invoice_ids" not in seller["sale.quotation"].browse(q.ids).read(["invoice_count"])[0]
    biller = env.with_user(
        user("seller-biller@sale.test", ["sale.group_sale_all", "account.group_account_invoice"]).ids[0]
    )
    moves = biller["sale.quotation"].browse(q.ids).action_create_invoice()
    assert len(moves["invoice_ids"]) == 1 and biller["sale.quotation"].browse(q.ids).invoice_count == 1
