"""Helpers for accounting tests."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.orm import Model

D = dt.date


def partner(env: Any, name: str = "Acme Corp", **vals: Any) -> Model:
    return env["contacts.partner"].create({"name": name, "is_company": True, "email": "ap@acme.test", **vals})


def make_tax(
    env: Any, name: str, amount: float, *, use: str = "sale", company: Model | None = None, **vals: Any
) -> Model:
    company = company or env.company
    account = env["account.account"].search(
        [("code", "=", "2110" if use == "sale" else "1410"), ("company_id", "=", company.ids[0])], limit=1
    )
    reps = [
        (
            "create",
            {
                "document_type": doc,
                "repartition_type": kind,
                "factor_percent": 100.0,
                **({"account_id": account.ids[0]} if kind == "tax" else {}),
            },
        )
        for doc in ("invoice", "refund")
        for kind in ("base", "tax")
    ]
    return env["account.tax"].create(
        {
            "name": name,
            "amount": amount,
            "type_tax_use": use,
            "company_id": company.ids[0],
            "repartition_line_ids": reps,
            **vals,
        }
    )


def invoice(
    env: Any,
    customer: Model,
    lines: list[dict[str, Any]],
    *,
    move_type: str = "out_invoice",
    post: bool = True,
    **vals: Any,
) -> Model:
    move = env["account.move"].create(
        {
            "move_type": move_type,
            "partner_id": customer.ids[0],
            "invoice_date": vals.pop("invoice_date", D(2026, 3, 10)),
            "invoice_line_ids": [("create", {"name": line.get("name", "Line"), **line}) for line in lines],
            **vals,
        }
    )
    if post:
        move.action_post()
    return move


def lines_by_type(move: Model) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for line in move.line_ids:
        out.setdefault(line.display_type, []).append((float(line.balance), float(line.amount_currency)))
    return out


def eu_env(env: Any) -> Any:
    """Admin working in the EUR company (EU chart), with company-specific USD rates."""
    company = env["core.company"].search([("chart_template", "=", "eu")], limit=1)
    if not company:
        company = env["core.company"].elevated().search([("chart_template", "=", "eu")], limit=1)
    env.user.elevated().write({"company_ids": [("add", company.ids[0])]})
    e = env.with_company(company)
    usd = e.ref("core.currency_usd") if e.ref("core.currency_usd", raise_if_not_found=False) else None
    if usd is None:
        usd = e["core.currency"].search([("name", "=", "USD")], limit=1)
    rates = e["core.currency_rate"].elevated()
    for day, rate in ((D(2026, 1, 1), 1.10), (D(2026, 3, 20), 1.05), (D(2026, 3, 31), 1.20)):
        rates.create({"currency_id": usd.ids[0], "company_id": company.ids[0], "date": day, "rate": rate})
    return e
