"""Access rights and company isolation of the sales models, and the sales demo scenario."""

from __future__ import annotations

from typing import Any

import pytest

from modules.account.tests.common import eu_env
from modules.sale.tests.common import customer, order, product
from tessera.exceptions import AccessError

pytestmark = pytest.mark.db


def _user(env: Any, login: str, groups: list[str], company: Any | None = None) -> Any:
    company = company or env.company
    return env["core.user"].create(
        {
            "name": login,
            "login": login,
            "company_id": company.ids[0],
            "company_ids": [("set", company.ids)],
            "group_ids": [("set", [env.ref(g).ids[0] for g in groups])],
        }
    )


def test_salespeople_read_configuration_administrators_change_it(env: Any) -> None:
    seller = env.with_user(_user(env, "cfg-seller@sale.test", ["sale.group_sale_user"]).ids[0])
    assert seller["sale.promotion"].search_count([]) >= 1
    assert seller["sale.order_template"].search_count([]) >= 1
    with pytest.raises(AccessError):
        seller["sale.promotion"].create({"name": "Mine", "reward_ids": []})
    with pytest.raises(AccessError):
        seller["sale.order_template"].create({"name": "Mine"})
    with pytest.raises(AccessError):
        seller["sale.promotion_reward"].search([], limit=1).write({"discount_percent": 99})
    manager = env.with_user(_user(env, "cfg-manager@sale.test", ["sale.group_sale_manager"]).ids[0])
    promo = manager["sale.promotion"].create(
        {"name": "Manager deal", "reward_ids": [("create", {"reward_type": "discount_order", "discount_percent": 3})]}
    )
    template = manager["sale.order_template"].create(
        {"name": "Manager kit", "option_ids": [("create", {"product_id": product(env).ids[0]})]}
    )
    assert promo.reward_ids and template.option_ids
    plain = env.with_user(_user(env, "cfg-plain@sale.test", ["core.group_user"]).ids[0])
    with pytest.raises(AccessError):
        plain["sale.promotion"].search([])


def test_company_isolation(env: Any) -> None:
    """U-4: a salesperson of the US company sees nothing of the EU company's sales."""
    eu = eu_env(env)
    eu_company = eu.company
    eu_order = order(eu, customer(eu, "Paris buyer"), [{"name": "Service", "price_unit": 10}])
    eu_template = eu["sale.order_template"].create({"name": "EU kit", "company_id": eu_company.ids[0]})
    eu_promo = eu["sale.promotion"].create(
        {
            "name": "EU deal",
            "company_id": eu_company.ids[0],
            "reward_ids": [("create", {"reward_type": "discount_order", "discount_percent": 3})],
        }
    )
    assert eu_order.company_id == eu_company and eu_order.line_ids.company_id == eu_company
    us_user = _user(env, "us-seller@sale.test", ["sale.group_sale_manager"], env.ref("core.main_company"))
    us = env.with_user(us_user.ids[0])
    assert not us["sale.quotation"].search([("id", "=", eu_order.ids[0])])
    assert not us["sale.quotation_line"].search([("id", "in", eu_order.line_ids.ids)])
    assert not us["sale.order_template"].search([("id", "=", eu_template.ids[0])])
    assert not us["sale.promotion"].search([("id", "=", eu_promo.ids[0])])
    assert not us["sale.promotion_reward"].search([("id", "in", eu_promo.reward_ids.ids)])


def test_demo_scenario(env: Any, system_env: Any) -> None:
    from modules.sale.demo import seed_sales

    created = seed_sales(system_env)
    assert created == 4
    assert seed_sales(system_env) == 0  # idempotent
    orders = system_env["sale.quotation"].search([("client_reference", "like", "DEMO-SO-")], order="client_reference")
    statuses = {o.client_reference: (o.state, o.invoice_status, o.locked) for o in orders}
    assert statuses["DEMO-SO-01"][:2] == ("accepted", "no")  # partly delivered and invoiced
    assert orders[0].invoice_count == 1
    deposit = orders.filtered(lambda o: o.client_reference == "DEMO-SO-02")
    assert deposit.line_ids.filtered(lambda r: r.is_downpayment) and deposit.invoice_status == "no"
    promo = orders.filtered(lambda o: o.client_reference == "DEMO-SO-03")
    assert promo.line_ids.filtered(lambda r: r.is_reward_line) and promo.state == "accepted"
    assert statuses["DEMO-SO-04"] == ("accepted", "invoiced", True)
