"""Pricelists: rule selection (variant > product > category > all, minimum quantity, dates, sequence), fixed /
discount / formula prices, rounding and margins, other pricelists and the recursion guard, currencies and
units, the pricelist of a partner, multi-company isolation."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from tessera.exceptions import AccessError, ValidationError

pytestmark = pytest.mark.db

ON = dt.date(2026, 6, 1)


@pytest.fixture
def cat(env: Any) -> SimpleNamespace:
    """Categories root > child > leaf, a widget in the leaf (100 / cost 60) and an empty USD pricelist."""
    categories = env["product.category"]
    root = categories.create({"name": "PL root"})
    child = categories.create({"name": "PL child", "parent_id": root.ids[0]})
    leaf = categories.create({"name": "PL leaf", "parent_id": child.ids[0]})
    widget = env["product.product"].create(
        {"name": "Widget", "list_price": 100, "standard_price": 60, "category_id": leaf.ids[0]}
    )
    pricelist = env["product.pricelist"].create({"name": "Test prices", "currency_id": env.ref("core.USD").ids[0]})
    return SimpleNamespace(root=root, child=child, leaf=leaf, widget=widget, pricelist=pricelist)


def _rule(pricelist: Any, **vals: Any) -> Any:
    return pricelist.env["product.pricelist_item"].create({"pricelist_id": pricelist.ids[0], **vals})


def test_rule_priority_levels(env: Any, cat: SimpleNamespace) -> None:
    pl, widget = cat.pricelist, cat.widget
    all_rule = _rule(pl, applied_on="all", compute_price="fixed", fixed_price=90)
    root_rule = _rule(pl, applied_on="category", category_id=cat.root.ids[0], fixed_price=80)
    child_rule = _rule(pl, applied_on="category", category_id=cat.child.ids[0], fixed_price=70)
    product_rule = _rule(pl, applied_on="product", product_id=widget.ids[0], fixed_price=60)
    assert pl._get_product_price(widget, 1, date=ON) == 60
    assert pl._get_product_rule(widget, 1, date=ON) == product_rule
    product_rule.unlink()
    assert pl._get_product_price(widget, 1, date=ON) == 70  # deepest category first
    assert pl._get_product_rule(widget, date=ON) == child_rule
    child_rule.unlink()
    assert pl._get_product_price(widget, 1, date=ON) == 80  # a parent category covers its sub-categories
    root_rule.unlink()
    assert pl._get_product_price(widget, 1, date=ON) == 90
    all_rule.unlink()
    assert pl._get_product_price(widget, 1, date=ON) == 100  # no rule: the sales price
    assert not pl._get_product_rule(widget, date=ON)


def test_variant_rule_beats_template_rule(env: Any, cat: SimpleNamespace) -> None:
    pl = cat.pricelist
    tshirt = env.ref("product.product_tshirt")
    red_l, blue_s = env.ref("product.product_tshirt_red_l"), env.ref("product.product_tshirt_blue_s")
    _rule(pl, applied_on="all", compute_price="percentage", percent_price=50)
    template_rule = _rule(pl, applied_on="product", product_id=tshirt.ids[0], fixed_price=20)
    assert pl._get_product_price(blue_s, date=ON) == 20
    assert pl._get_product_price(red_l, date=ON) == 22  # the template's fixed price plus the L extra
    assert pl._get_product_rule(red_l, date=ON) == template_rule
    variant_rule = _rule(pl, applied_on="variant", variant_id=red_l.ids[0], fixed_price=18)
    _rule(pl, applied_on="product", product_id=red_l.ids[0], fixed_price=19)
    assert pl._get_product_price(red_l, date=ON) == 18
    assert pl._get_product_rule(red_l, date=ON) == variant_rule
    assert pl._get_product_price(blue_s, date=ON) == 20


def test_minimum_quantity_and_sequence(env: Any, cat: SimpleNamespace) -> None:
    pl, widget = cat.pricelist, cat.widget
    _rule(pl, applied_on="all", min_quantity=10, compute_price="percentage", percent_price=10)
    _rule(pl, applied_on="all", min_quantity=20, compute_price="percentage", percent_price=20)
    _rule(pl, applied_on="category", category_id=cat.root.ids[0], min_quantity=100, fixed_price=50)
    assert pl._get_product_price(widget, 5, date=ON) == 100
    assert pl._get_product_price(widget, 10, date=ON) == 90
    assert pl._get_product_price(widget, 25, date=ON) == 80  # the higher minimum first
    assert pl._get_product_price(widget, 100, date=ON) == 50  # more specific wins once reached
    _rule(pl, applied_on="all", min_quantity=20, compute_price="percentage", percent_price=30, sequence=5)
    assert pl._get_product_price(widget, 25, date=ON) == 70  # same level and minimum: sequence


def test_date_window(env: Any, cat: SimpleNamespace) -> None:
    pl, widget = cat.pricelist, cat.widget
    _rule(pl, applied_on="all", fixed_price=70, date_start="2026-06-01", date_end="2026-06-30")
    assert pl._get_product_price(widget, date=dt.date(2026, 6, 1)) == 70
    assert pl._get_product_price(widget, date="2026-06-30") == 70
    assert pl._get_product_price(widget, date=dt.datetime(2026, 6, 15, 23, 0, tzinfo=dt.UTC)) == 70
    assert pl._get_product_price(widget, date=dt.date(2026, 5, 31)) == 100
    assert pl._get_product_price(widget, date=dt.date(2026, 7, 1)) == 100
    _rule(pl, applied_on="all", fixed_price=65, date_start="2026-07-01")
    assert pl._get_product_price(widget, date=dt.date(2027, 1, 1)) == 65


def test_formula_discount_rounding_surcharge(env: Any, cat: SimpleNamespace) -> None:
    pl, widget = cat.pricelist, cat.widget
    rule = _rule(pl, applied_on="all", compute_price="formula", price_discount=15, price_round=10, price_surcharge=-1)
    assert pl._get_product_price(widget, date=ON) == 89  # 100 - 15 % = 85 → 90 → 89
    rule.write({"price_round": 0.05, "price_discount": 12.34, "price_surcharge": 0})
    assert pl._get_product_price(widget, date=ON) == 87.65  # 87.66 → 87.65
    rule.write({"base": "standard_price", "price_discount": -25, "price_round": 0})
    assert pl._get_product_price(widget, date=ON) == 75  # cost plus 25 %


def test_formula_margins(env: Any, cat: SimpleNamespace) -> None:
    pl, widget = cat.pricelist, cat.widget  # cost 60
    rule = _rule(pl, applied_on="all", compute_price="formula", price_discount=50)
    assert pl._get_product_price(widget, date=ON) == 50
    rule.write({"price_min_margin": 0})
    assert pl._get_product_price(widget, date=ON) == 60  # a zero margin is a margin: never below cost
    rule.write({"price_min_margin": 10})
    assert pl._get_product_price(widget, date=ON) == 70
    rule.write({"price_discount": 0, "price_min_margin": None, "price_max_margin": 20})
    assert pl._get_product_price(widget, date=ON) == 80
    rule.write({"price_min_margin": 5})
    assert pl._get_product_price(widget, date=ON) == 80


def test_percentage_on_cost(env: Any, cat: SimpleNamespace) -> None:
    _rule(cat.pricelist, applied_on="all", compute_price="percentage", percent_price=-50, base="standard_price")
    assert cat.pricelist._get_product_price(cat.widget, date=ON) == 90


def test_based_on_another_pricelist_and_recursion(env: Any, cat: SimpleNamespace) -> None:
    widget, a = cat.widget, cat.pricelist
    b = env["product.pricelist"].create({"name": "Base prices", "currency_id": env.ref("core.USD").ids[0]})
    _rule(b, applied_on="all", compute_price="percentage", percent_price=10)
    on_b = _rule(
        a, applied_on="all", compute_price="formula", base="pricelist", base_pricelist_id=b.ids[0], price_discount=10
    )
    assert a._get_product_price(widget, date=ON) == 81  # 100 → 90 on B → 81
    with pytest.raises(ValidationError, match="based on itself"):
        _rule(b, applied_on="all", compute_price="percentage", base="pricelist", base_pricelist_id=a.ids[0])
    with pytest.raises(ValidationError, match="based on itself"):
        on_b.write({"base_pricelist_id": a.ids[0]})
    with pytest.raises(ValidationError, match="Choose the pricelist"):
        _rule(a, applied_on="all", compute_price="formula", base="pricelist")
    # a fixed price ignores its base: no loop
    _rule(b, applied_on="all", compute_price="fixed", fixed_price=1, base="pricelist", base_pricelist_id=a.ids[0])


def test_recursion_guard_at_computation(env: Any, cat: SimpleNamespace) -> None:
    widget, a = cat.widget, cat.pricelist
    b = env["product.pricelist"].create({"name": "Loop", "currency_id": env.ref("core.USD").ids[0]})
    _rule(a, applied_on="all", compute_price="formula", base="pricelist", base_pricelist_id=b.ids[0])
    loop = _rule(b, applied_on="all", compute_price="formula")
    env.cr.execute(
        "UPDATE product_pricelist_item SET base = 'pricelist', base_pricelist_id = :a WHERE id = :id",
        {"a": a.ids[0], "id": loop.ids[0]},
    )
    env.cache.invalidate("product.pricelist_item")
    with pytest.raises(ValidationError, match="based on itself through other pricelists"):
        a._get_product_price(widget, date=ON)


def test_currencies(env: Any, cat: SimpleNamespace) -> None:
    usd, eur = env.ref("core.USD"), env.ref("core.EUR")
    euro = env["product.pricelist"].create({"name": "Euro", "currency_id": eur.ids[0]})
    assert euro._get_product_price(cat.widget, date=ON) == 92  # 100 USD at 0.92
    assert euro._get_product_price(cat.widget, date=ON, currency=usd) == 100
    _rule(euro, applied_on="all", fixed_price=46)
    assert euro._get_product_price(cat.widget, date=ON) == 46
    assert euro._get_product_price(cat.widget, date=ON, currency=usd) == 50
    # based on a USD pricelist: its price is converted first
    _rule(cat.pricelist, applied_on="all", compute_price="percentage", percent_price=50)
    based = env["product.pricelist"].create(
        {
            "name": "Euro on USD",
            "currency_id": eur.ids[0],
            "item_ids": [
                (
                    "create",
                    {
                        "compute_price": "formula",
                        "base": "pricelist",
                        "base_pricelist_id": cat.pricelist.ids[0],
                        "price_surcharge": 1,
                    },
                )
            ],
        }
    )
    assert based._get_product_price(cat.widget, date=ON) == 47  # 50 USD = 46 EUR, + 1


def test_units(env: Any, cat: SimpleNamespace) -> None:
    dozen = env.ref("product.uom_dozen")
    _rule(cat.pricelist, applied_on="all", min_quantity=10, compute_price="percentage", percent_price=10)
    assert cat.pricelist._get_product_price(cat.widget, 1, uom=dozen, date=ON) == 1080  # 12 units reach 10
    assert cat.pricelist._get_product_price(cat.widget, 0.5, uom=dozen, date=ON) == 1200
    assert cat.pricelist._get_product_price(cat.widget, 12, date=ON) == 90


def test_demo_pricelists(env: Any) -> None:
    wholesale, europe = env.ref("product.pricelist_wholesale"), env.ref("product.pricelist_europe")
    arm = env.ref("product.product_robot_arm")
    assert wholesale._get_product_price(arm, 1, date=ON) == 18900
    assert wholesale._get_product_price(arm, 10, date=ON) == 17010
    assert wholesale._get_product_price(arm, 50, date=ON) == 16065
    assert europe.currency_id.name == "EUR" and set(europe.country_ids.mapped("code")) == {"FR", "DE"}
    assert europe._get_product_price(arm, date=ON) == 17387.99  # 17,388 EUR, rounded, minus 0.01
    assert europe._get_product_price(env.ref("product.product_tshirt_red_l"), date=ON) == 24.99


def test_pricelist_for_partner(env: Any) -> None:
    partners = env["contacts.partner"]
    public, wholesale, europe = (
        env.ref("product.pricelist_public"),
        env.ref("product.pricelist_wholesale"),
        env.ref("product.pricelist_europe"),
    )
    pricelists = env["product.pricelist"]
    french = partners.create({"name": "Client FR", "country_id": env.ref("core.country_fr").ids[0]})
    american = partners.create({"name": "Client US", "country_id": env.ref("core.country_us").ids[0]})
    german_co = partners.create({"name": "GmbH", "is_company": True, "country_id": env.ref("core.country_de").ids[0]})
    german_contact = partners.create({"name": "Hans", "parent_id": german_co.ids[0]})
    # ``pricelist_id`` is added by sale; these partners have none of their own
    assert "pricelist_id" not in partners._fields or not (french | american | german_contact).mapped("pricelist_id")
    assert pricelists._for_partner(french) == europe
    assert pricelists._for_partner(german_contact) == europe  # the company's country
    assert pricelists._for_partner(american) == public  # the default prefers lists without countries
    assert pricelists._for_partner(None) == public
    eu_company = env.ref("core.demo_company_eu")
    assert pricelists._for_partner(american, eu_company) == europe  # nothing else is shared with it
    # the partner's own pricelist when the field exists (sale adds it)
    own = SimpleNamespace(_fields={"pricelist_id": None}, pricelist_id=wholesale, country_id=french.country_id)
    own.ensure_one = lambda: None
    assert pricelists._for_partner(own) == wholesale
    assert pricelists._for_partner(own, eu_company) == europe  # another company's list is ignored
    wholesale.action_archive()
    assert pricelists._for_partner(own) == europe
    for pl in pricelists.search([]):
        pl.action_archive()
    assert not pricelists._for_partner(french)


def test_multi_company_isolation(env: Any, env_for: Any) -> None:
    eu_company = env.ref("core.demo_company_eu")
    eu_list = (
        env["product.pricelist"]
        .elevated()
        .create(
            {
                "name": "Paris shop",
                "currency_id": env.ref("core.EUR").ids[0],
                "company_id": eu_company.ids[0],
                "item_ids": [("create", {"applied_on": "all", "fixed_price": 10})],
            }
        )
    )
    main_only = env["core.user"].create(
        {
            "login": "pricing_user",
            "name": "Pricing User",
            "company_id": env.ref("core.main_company").ids[0],
            "company_ids": [env.ref("core.main_company").ids[0]],
            "group_ids": [env.ref("core.group_user").ids[0]],
        }
    )
    us = env.with_user(main_only.ids[0])
    visible = us["product.pricelist"].search([])
    assert eu_list not in visible and env.ref("product.pricelist_wholesale") in visible
    assert env.ref("product.pricelist_europe") in visible  # shared
    assert not us["product.pricelist_item"].search([("pricelist_id", "=", eu_list.ids[0])])
    assert eu_list.item_ids.company_id == eu_company  # elevated: admin works in the main company only
    marie = env_for("marie")
    marie_lists = marie["product.pricelist"].search([])
    assert eu_list in marie_lists and env.ref("product.pricelist_wholesale") not in marie_lists
    arm = env.ref("product.product_robot_arm")
    with pytest.raises(AccessError):
        marie["product.pricelist"].browse(env.ref("product.pricelist_wholesale").ids)._get_product_price(arm, 10)
    assert marie["product.pricelist"].browse(eu_list.ids)._get_product_price(arm) == 10


def test_users_price_but_do_not_edit(env: Any, env_for: Any) -> None:
    demo = env_for("demo")
    wholesale = demo["product.pricelist"].browse(env.ref("product.pricelist_wholesale").ids)
    arm = demo["product.product"].browse(env.ref("product.product_robot_arm").ids)
    rule = wholesale._get_product_rule(arm, 50, date=ON)
    assert rule.percent_price == 15
    margin = env["product.pricelist_item"].create(
        {
            "pricelist_id": wholesale.ids[0],
            "applied_on": "all",
            "min_quantity": 1000,
            "compute_price": "formula",
            "price_discount": 90,
            "price_min_margin": 100,
        }
    )
    assert margin and wholesale._get_product_price(arm, 1000, date=ON) == 11300  # cost read for the user
    with pytest.raises(AccessError):
        demo["product.pricelist"].create({"name": "Mine"})


@pytest.mark.parametrize(
    ("vals", "message"),
    [
        ({"applied_on": "category"}, "Choose the category"),
        ({"applied_on": "product"}, "Choose the product"),
        ({"applied_on": "variant"}, "Choose the variant"),
        ({"date_start": "2026-02-01", "date_end": "2026-01-01"}, "ends before it starts"),
        ({"min_quantity": -1}, "minimum quantity cannot be negative"),
        ({"compute_price": "percentage", "percent_price": 120}, "cannot exceed 100"),
        ({"compute_price": "formula", "price_round": -1}, "rounding cannot be negative"),
        ({"compute_price": "formula", "price_min_margin": 10, "price_max_margin": 5}, "above the maximum"),
    ],
)
def test_rule_constraints(env: Any, cat: SimpleNamespace, vals: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _rule(cat.pricelist, **vals)


def test_rule_names(env: Any, cat: SimpleNamespace) -> None:
    assert _rule(cat.pricelist, applied_on="all").display_name == "All products"
    assert _rule(cat.pricelist, applied_on="all", min_quantity=10).display_name == "All products (from 10)"
    assert (
        _rule(cat.pricelist, applied_on="category", category_id=cat.child.ids[0]).display_name
        == "Category: PL root / PL child"
    )
    assert _rule(cat.pricelist, applied_on="product", product_id=cat.widget.ids[0]).display_name == "Widget"
    red_l = env.ref("product.product_tshirt_red_l")
    assert _rule(cat.pricelist, applied_on="variant", variant_id=red_l.ids[0]).display_name == red_l.display_name
