"""Quotation templates, optional products suggested from the products, and customers adding options online."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

from modules.sale.tests.common import customer, order, product, product_lines
from tessera.exceptions import UserError, ValidationError

pytestmark = pytest.mark.db


@pytest.fixture
def kit(env: Any) -> dict[str, Any]:
    arm = product(env, "Test arm", price=1000)
    gripper = product(env, "Test gripper", price=200)
    sensor = product(env, "Test sensor", price=300)
    support = product(env, "Test support", price=50, type="service")
    template = env["sale.order_template"].create(
        {
            "name": "Test kit",
            "number_of_days": 10,
            "payment_term_id": env.ref("account.term_45").ids[0],
            "note": "<p>Kit terms</p>",
            "line_ids": [
                ("create", {"display_type": "section", "name": "Hardware", "sequence": 1}),
                ("create", {"product_id": arm.ids[0], "quantity": 2, "sequence": 2}),
                ("create", {"product_id": gripper.ids[0], "quantity": 1, "discount": 10, "sequence": 3}),
                ("create", {"name": "Delivery by truck", "quantity": 1, "sequence": 4}),
            ],
            "option_ids": [("create", {"product_id": support.ids[0], "quantity": 1})],
        }
    )
    return {"arm": arm, "gripper": gripper, "sensor": sensor, "support": support, "template": template}


def test_template_applied_on_create(env: Any, kit: dict[str, Any]) -> None:
    q = env["sale.quotation"].create({"partner_id": customer(env).ids[0], "template_id": kit["template"].ids[0]})
    names = [line.name for line in q.line_ids]
    assert "Hardware" in names and "Delivery by truck" in names
    assert q.validity_date == env.today() + dt.timedelta(days=10)
    assert q.note == "<p>Kit terms</p>" and q.payment_term_id == env.ref("account.term_45")
    option = q.line_ids.filtered(lambda line: line.is_optional)
    assert option.product_id == kit["support"]
    assert q.amount_untaxed == 2 * 1000 + 180  # the option is not in the totals; the free text line is 0


def test_apply_template_replaces_the_lines(env: Any, kit: dict[str, Any]) -> None:
    q = order(env, customer(env), [(kit["sensor"], 1)])
    q.write({"template_id": kit["template"].ids[0]})
    q.action_apply_template()
    assert kit["sensor"] not in q.line_ids.mapped("product_id")
    assert kit["arm"] in q.line_ids.mapped("product_id")
    other = env["sale.order_template"].create(
        {"name": "Sensor only", "line_ids": [("create", {"product_id": kit["sensor"].ids[0], "quantity": 4})]}
    )
    q.action_apply_template(template_id=other.ids[0])
    assert q.template_id == other and product_lines(q).product_id == kit["sensor"] and product_lines(q).quantity == 4
    q.action_confirm()
    with pytest.raises(UserError, match="draft or sent"):
        q.action_apply_template()
    with pytest.raises(UserError, match="Choose a quotation template"):
        order(env, customer(env, "No template"), []).action_apply_template()


def test_template_lines_are_checked(env: Any) -> None:
    with pytest.raises(ValidationError):
        env["sale.order_template"].create({"name": "Bad", "line_ids": [("create", {"quantity": 1})]})
    with pytest.raises(ValidationError):
        env["sale.order_template"].create({"name": "Bad days", "number_of_days": -1})


def test_optional_products_are_suggested_once(env: Any, kit: dict[str, Any]) -> None:
    kit["arm"].write(
        {"optional_product_ids": [("set", kit["sensor"].ids)], "accessory_product_ids": [("set", kit["gripper"].ids)]}
    )
    q = order(env, customer(env), [(kit["arm"], 1)])
    options = q.line_ids.filtered(lambda line: line.is_optional)
    assert options.product_id == kit["sensor"] and options.quantity == 1
    assert q.amount_untaxed == 1000
    q.write({"line_ids": [("create", {"product_id": kit["arm"].ids[0], "quantity": 1})]})
    assert len(q.line_ids.filtered(lambda line: line.is_optional)) == 1  # not offered twice
    assert q._suggested_products() == kit["gripper"]  # accessories are upsell candidates
    with pytest.raises(ValidationError):
        kit["arm"].write({"optional_product_ids": [("set", kit["arm"].ids)]})


def test_customer_adds_an_option(env: Any, kit: dict[str, Any]) -> None:
    q = env["sale.quotation"].create({"partner_id": customer(env).ids[0], "template_id": kit["template"].ids[0]})
    option = q.line_ids.filtered(lambda line: line.is_optional)
    with pytest.raises(UserError, match="waiting for your answer"):
        q.add_optional_line(option.ids[0])
    q.action_send()
    before = q.amount_untaxed
    q.add_optional_line(option.ids[0])
    assert not option.is_optional and q.amount_untaxed == before + 50
    with pytest.raises(UserError, match="not available"):
        q.add_optional_line(option.ids[0])


def test_portal_optional_endpoint(client, admin) -> None:  # type: ignore[no-untyped-def]
    partner = admin.post(
        "/api/v1/models/contacts.partner", json={"values": {"name": "Option Buyer", "email": "ob@example.test"}}
    ).json()
    q = admin.post(
        "/api/v1/models/sale.quotation",
        json={
            "values": {
                "partner_id": partner["id"],
                "line_ids": [
                    ["create", {"name": "Main", "price_unit": 100, "quantity": 1}],
                    ["create", {"name": "Extra warranty", "price_unit": 30, "quantity": 1, "is_optional": True}],
                ],
            }
        },
    ).json()
    sent = admin.post(f"/api/v1/models/sale.quotation/{q['id']}/call/action_send", json={}).json()["result"]
    token = sent["portal_url"].split("token=")[1]
    view = client.get(f"/api/v1/sale/quotes/{q['id']}", params={"token": token}).json()
    assert view["can_add_options"] is True and view["amount_untaxed"] == 100
    option = next(line for line in view["lines"] if line["is_optional"])
    assert option["taxes"] == [] and "tax_percent" not in option
    denied = client.post(f"/api/v1/sale/quotes/{q['id']}/optional/{option['id']}", json={"token": "bad"})
    assert denied.status_code == 401
    added = client.post(f"/api/v1/sale/quotes/{q['id']}/optional/{option['id']}", json={"token": token}).json()
    assert added["amount_untaxed"] == 130 and not any(line["is_optional"] for line in added["lines"])
    again = client.post(f"/api/v1/sale/quotes/{q['id']}/optional/{option['id']}", json={"token": token})
    assert again.status_code == 400
