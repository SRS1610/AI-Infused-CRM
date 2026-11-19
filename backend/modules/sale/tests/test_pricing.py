"""Prices and taxes on sales orders: customer defaults, pricelist prices, typed prices, fiscal positions,
tax-included prices and the sellable-product rule."""

from __future__ import annotations

from typing import Any

import pytest

from modules.sale.tests.common import customer, order, product, product_lines, sale_tax
from tessera.exceptions import ValidationError

pytestmark = pytest.mark.db


def _position(env: Any, src: Any, dest: Any, name: str = "Test position") -> Any:
    return env["account.fiscal_position"].create(
        {"name": name, "tax_ids": [("create", {"tax_src_id": src.ids[0], "tax_dest_id": dest.ids[0]})]}
    )


def test_customer_defaults_on_create(env: Any) -> None:
    wholesale = env.ref("product.pricelist_wholesale")
    term = env.ref("account.term_30")
    tax = sale_tax(env, 20)
    position = _position(env, tax, sale_tax(env, 0, "Test exempt"))
    buyer = customer(
        env,
        pricelist_id=wholesale.ids[0],
        property_payment_term_id=term.ids[0],
        property_account_position_id=position.ids[0],
    )
    q = order(env, buyer, [])
    assert (q.pricelist_id, q.payment_term_id, q.fiscal_position_id) == (wholesale, term, position)
    assert q.currency_id.name == "USD"
    contact = env["contacts.partner"].create({"name": "Buyer contact", "parent_id": buyer.ids[0]})
    assert order(env, contact, []).payment_term_id == term  # the company's settings apply to its contacts
    plain = order(env, customer(env, "No settings"), [])
    assert plain.pricelist_id == env.ref("product.pricelist_public") and not plain.fiscal_position_id


def test_regional_pricelist_gives_its_currency(env: Any) -> None:
    french = customer(env, "Lyon Robotique", country_id=env.ref("core.country_fr").ids[0])
    q = order(env, french, [(product(env, price=100), 1)])
    assert q.pricelist_id == env.ref("product.pricelist_europe")
    assert q.currency_id.name == "EUR" and q.line_ids.currency_id.name == "EUR"
    assert q.fiscal_position_id  # export position detected from the country (US company)


def test_pricelist_prices_follow_the_quantity_unless_typed(env: Any) -> None:
    buyer = customer(env, pricelist_id=env.ref("product.pricelist_wholesale").ids[0])
    widget = product(env, price=100)
    q = order(env, buyer, [(widget, 1)])
    line = product_lines(q)
    assert line.price_unit == 100 and not line.price_manual
    line.write({"quantity": 10})
    assert line.price_unit == 90  # 10 % from 10 units
    line.write({"quantity": 50})
    assert line.price_unit == 85
    line.write({"price_unit": 77})
    assert line.price_manual
    line.write({"quantity": 60})
    assert line.price_unit == 77  # a typed price is kept
    line.write({"price_unit": None})  # back to the pricelist
    assert line.price_unit == 85 and not line.price_manual
    typed = order(env, buyer, [{"product_id": widget.ids[0], "quantity": 10, "price_unit": 55}])
    assert product_lines(typed).price_unit == 55 and product_lines(typed).price_manual


def test_changing_pricelist_or_customer_reprices(env: Any) -> None:
    widget = product(env, price=100)
    q = order(env, customer(env), [(widget, 10)])
    assert product_lines(q).price_unit == 100
    q.write({"pricelist_id": env.ref("product.pricelist_wholesale").ids[0]})
    assert product_lines(q).price_unit == 90 and q.amount_untaxed == 900
    other = customer(env, "Public buyer", pricelist_id=env.ref("product.pricelist_public").ids[0])
    q.write({"partner_id": other.ids[0]})
    assert q.pricelist_id == env.ref("product.pricelist_public") and product_lines(q).price_unit == 100


def test_taxes_come_from_the_product_mapped_by_the_fiscal_position(env: Any) -> None:
    standard, reduced = sale_tax(env, 20), sale_tax(env, 5)
    widget = product(env, price=100, taxes=standard)
    position = _position(env, standard, reduced)
    q = order(env, customer(env), [(widget, 1)])
    line = product_lines(q)
    assert line.tax_ids == standard and q.amount_tax == 20 and q.amount_total == 120
    q.write({"fiscal_position_id": position.ids[0]})
    assert line.tax_ids == reduced and q.amount_tax == 5
    q.write({"fiscal_position_id": None})
    assert line.tax_ids == standard
    other = product(env, "Gadget", price=10, taxes=reduced)
    line.write({"product_id": other.ids[0]})
    assert line.tax_ids == reduced and line.price_unit == 10 and line.name == "Gadget"


def test_tax_included_price_discount_and_several_taxes(env: Any) -> None:
    included = sale_tax(env, 20, "Test VAT 20% incl", price_include=True)
    widget = product(env, price=120, taxes=included)
    q = order(env, customer(env), [{"product_id": widget.ids[0], "quantity": 1, "discount": 10}])
    line = product_lines(q)
    assert (line.price_subtotal, line.price_tax, line.price_total) == (90.0, 18.0, 108.0)
    state, local = sale_tax(env, 6), sale_tax(env, 2.5)
    line.write({"tax_ids": [("set", [state.ids[0], local.ids[0]])], "discount": 0})
    assert (
        line.price_subtotal == 120 and line.price_tax == pytest.approx(10.2) and q.amount_total == pytest.approx(130.2)
    )


def test_only_sellable_products_on_lines(env: Any) -> None:
    q = order(env, customer(env), [])
    assert q.line_ids._fields["product_id"].domain == [["is_sellable", "=", True]]
    hidden = product(env, "Internal part", sale_ok=False)
    with pytest.raises(ValidationError, match="cannot be sold"):
        q.write({"line_ids": [("create", {"product_id": hidden.ids[0]})]})
    tshirt = env.ref("product.product_tshirt")  # a template: its variants are sold
    with pytest.raises(ValidationError, match="cannot be sold"):
        q.write({"line_ids": [("create", {"product_id": tshirt.ids[0]})]})
    q.write({"line_ids": [("create", {"product_id": tshirt.variant_ids[0].ids[0]})]})
    line = product_lines(q)
    with pytest.raises(ValidationError, match="cannot be sold"):
        line.write({"product_id": hidden.ids[0]})


def test_quantities_and_returns_are_validated(env: Any) -> None:
    widget = product(env)
    with pytest.raises(ValidationError, match="discount"):
        order(env, customer(env), [{"product_id": widget.ids[0], "discount": 120}])
    with pytest.raises(ValidationError, match="cannot be negative"):
        order(env, customer(env), [{"product_id": widget.ids[0], "qty_delivered": -1}])
    with pytest.raises(ValidationError, match="More is returned"):
        order(env, customer(env), [{"product_id": widget.ids[0], "quantity": 2, "qty_returned": 3}])
