"""Sales orders: confirmation by the salesperson, the *Sales order* label, locking, cancellation, the CRM hook
and the sales views."""

from __future__ import annotations

from typing import Any

import pytest

from modules.sale.tests.common import customer, order, product, product_lines
from tessera.exceptions import AccessError, ConflictError, UserError

pytestmark = pytest.mark.db


def test_salesperson_confirms_without_signature(env: Any) -> None:
    q = order(env, customer(env), [(product(env), 2)])
    assert dict(q._fields["state"].get_selection(q))["accepted"] == "Sales order"
    assert q.invoice_status == "no"
    q.action_confirm()
    assert q.state == "accepted" and q.confirmation_date and not q.signed_by
    assert q.invoice_status == "to_invoice"
    with pytest.raises(UserError, match="Only draft or sent"):
        q.action_confirm()
    empty = order(env, customer(env, "Empty"), [])
    with pytest.raises(UserError, match="at least one line"):
        empty.action_confirm()


def test_portal_acceptance_also_makes_an_order(env: Any) -> None:
    q = order(env, customer(env), [(product(env), 1)])
    q.action_send()
    q.accept("Jordan Lee")
    assert q.state == "accepted" and q.confirmation_date and q.invoice_status == "to_invoice"


def test_confirmation_wins_the_opportunity(env: Any) -> None:
    buyer = customer(env)
    lead = env["crm.lead"].create({"name": "Robot cell", "type": "opportunity", "partner_id": buyer.ids[0]})
    q = env["sale.quotation"].browse([lead.action_new_quotation()["quotation_id"]])
    q.write({"line_ids": [("create", {"product_id": product(env, price=500).ids[0], "quantity": 2})]})
    q.action_confirm()
    assert lead.is_won and lead.expected_revenue == 1000


def test_lock_freezes_the_order(env: Any) -> None:
    q = order(env, customer(env), [(product(env), 3)])
    with pytest.raises(UserError, match="Only sales orders"):
        q.action_lock()
    q.action_confirm()
    q.action_lock()
    assert q.locked
    line = product_lines(q)
    with pytest.raises(UserError, match="locked"):
        q.write({"partner_id": customer(env, "Other").ids[0]})
    with pytest.raises(UserError, match="locked"):
        line.write({"quantity": 5})
    with pytest.raises(UserError, match="locked"):
        q.write({"line_ids": [("create", {"name": "Extra", "price_unit": 1})]})
    with pytest.raises(UserError, match="locked"):
        line.unlink()
    with pytest.raises(UserError, match="locked"):
        q.action_cancel()
    line.write({"qty_delivered": 3, "qty_returned": 1})  # deliveries and returns are still recorded
    q.write({"note": "<p>Ship on pallets</p>"})
    q.action_unlock()
    line.write({"quantity": 5})
    assert q.amount_untaxed == 500


def test_cancel_rules(env: Any) -> None:
    q = order(env, customer(env), [(product(env), 1)])
    q.action_confirm()
    q.action_cancel()  # a confirmed order without invoices can be cancelled
    assert q.state == "cancelled" and q.invoice_status == "no"
    q.action_draft()
    assert q.state == "draft"
    with pytest.raises(ConflictError):
        q.write({"state": "accepted"})  # the workflow still guards the state


def test_plain_user_cannot_confirm(env: Any) -> None:
    plain = env["core.user"].create(
        {"name": "Plain", "login": "plain-sale@test", "group_ids": [("set", [env.ref("core.group_user").ids[0]])]}
    )
    q = order(env, customer(env), [(product(env), 1)])
    with pytest.raises(AccessError):
        q.with_user(plain.ids[0]).action_confirm()


def test_sales_views_and_menus(env: Any) -> None:
    from modules.web.views import resolve_view

    form = resolve_view(env, "sale.quotation", "form")
    text = str(form)
    for method in ("action_confirm", "action_create_invoice", "action_lock", "action_apply_promotions"):
        assert method in text
    assert "tax_ids" in text and "qty_delivered" in text and "invoice_ids" in text
    product_form = str(resolve_view(env, "product.product", "form"))
    assert "taxes_id" in product_form and "invoice_policy" in product_form and "tax_percent" not in product_form
    assert "pricelist_id" in str(resolve_view(env, "contacts.partner", "form"))
    names = {m.name for m in env["web.menu"].search([("parent_id", "child_of", env.ref("sale.menu_root").ids[0])])}
    assert {"Quotations", "Orders", "Orders to invoice", "Customers", "Pricelists", "Promotions"} <= names
    assert {"Quotation templates", "Sales analysis"} <= names
    to_invoice = env.ref("sale.action_orders_to_invoice")
    assert to_invoice.domain == [["invoice_status", "=", "to_invoice"]]
