"""Helpers for sales tests: customers, products with taxes, orders, and a clean promotion slate."""

from __future__ import annotations

from typing import Any

from modules.account.tests.common import make_tax
from tessera.orm import Model


def customer(env: Any, name: str = "Sale Customer", **vals: Any) -> Model:
    return env["contacts.partner"].create({"name": name, "is_company": True, "email": "buyer@sale.test", **vals})


def sale_tax(env: Any, rate: float, name: str | None = None, **vals: Any) -> Model:
    return make_tax(env, name or f"Test sales tax {rate:g}%", rate, use="sale", **vals)


def product(env: Any, name: str = "Widget", price: float = 100.0, taxes: Model | None = None, **vals: Any) -> Model:
    values: dict[str, Any] = {"name": name, "list_price": price, "type": "goods", **vals}
    values["taxes_id"] = [("set", taxes.ids if taxes is not None else [])]
    return env["product.product"].create(values)


def order(env: Any, partner: Model, lines: list[tuple[Model, float]] | list[dict[str, Any]], **vals: Any) -> Model:
    commands = []
    for entry in lines:
        if isinstance(entry, dict):
            commands.append(("create", entry))
        else:
            prod, qty = entry
            commands.append(("create", {"product_id": prod.ids[0], "quantity": qty}))
    return env["sale.quotation"].create({"partner_id": partner.ids[0], "line_ids": commands, **vals})


def no_promotions(env: Any) -> None:
    """Archive the demo promotions so a test controls which ones apply."""
    env["sale.promotion"].elevated().search([]).write({"active": False})


def product_lines(q: Model) -> Model:
    return q.line_ids.filtered(lambda line: not line.display_type and not line.is_optional and not line.is_reward_line)
