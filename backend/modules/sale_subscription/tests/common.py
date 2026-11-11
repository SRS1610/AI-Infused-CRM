"""Helpers for subscription tests: plans, recurring products, subscriptions, saved cards and the job."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.sale_subscription import billing
from tessera.orm import Model

D = dt.date


def at(env: Any, day: dt.date) -> Any:
    """``env`` working on the business date ``day``."""
    return env.with_context(**{billing.TODAY_KEY: day.isoformat()})


def plan(env: Any, unit: str = "month", value: int = 1, **vals: Any) -> Model:
    return env["sale.subscription_plan"].create(
        {
            "name": f"Test {value} {unit}",
            "billing_period_unit": unit,
            "billing_period_value": value,
            "renewal_reminder_days": 0,
            "auto_close_limit": 0,
            **vals,
        }
    )


def recurring(
    env: Any, name: str = "Cloud seat", price: float = 100.0, taxes: Model | None = None, **vals: Any
) -> Model:
    return env["product.product"].create(
        {
            "name": name,
            "list_price": price,
            "type": "service",
            "recurring_invoice": True,
            "taxes_id": [("set", taxes.ids if taxes is not None else [])],
            **vals,
        }
    )


def customer(env: Any, name: str = "Subscriber", **vals: Any) -> Model:
    return env["contacts.partner"].create({"name": name, "is_company": True, "email": "sub@subs.test", **vals})


def subscription(
    env: Any,
    partner: Model,
    the_plan: Model,
    lines: list[dict[str, Any]],
    start: dt.date,
    *,
    confirm: bool = True,
    **vals: Any,
) -> Model:
    e = at(env, start)
    sub = e["sale.quotation"].create(
        {
            "partner_id": partner.ids[0],
            "plan_id": the_plan.ids[0],
            "start_date": start,
            "date_order": dt.datetime.combine(start, dt.time(9, 0)),
            "validity_date": start + dt.timedelta(days=30),
            "line_ids": [("create", line) for line in lines],
            **vals,
        }
    )
    if confirm:
        sub.action_confirm()
    return sub


def run(env: Any, day: dt.date) -> dict[str, Any]:
    """The daily job on ``day`` (as the system, for the current company)."""
    return billing.run_recurring(env.elevated(), day, env.company)


def provider(env: Any) -> Model:
    """The current company's test payment provider, switched on."""
    providers = env["payment.provider"].search([("company_id", "=", env.company.ids[0])])
    test = env.ref(f"payment.{env.company.ids[0]}_provider_test")
    (providers - test).write({"state": "disabled"})
    test.write({"state": "test", "allow_tokenization": True})
    return test


def token(env: Any, partner: Model, ref: str = "good-card") -> Model:
    """A saved card; references containing ``fail`` are declined by the test provider."""
    return env["payment.token"].create(
        {
            "provider_id": provider(env).ids[0],
            "partner_id": partner.commercial_partner_id.ids[0],
            "provider_ref": ref,
            "display": "•••• 4242",
        }
    )


def docs(sub: Model) -> list[Model]:
    """The subscription's posted documents in creation order."""
    moves = sub.env["account.move"].search([("subscription_id", "=", sub.ids[0]), ("state", "=", "posted")], order="id")
    return list(moves)


def history(sub: Model) -> list[tuple[str, str, float]]:
    logs = sub.env["sale.subscription_log"].search([("subscription_id", "=", sub.ids[0])], order="event_date, id")
    return [(e.event_date.isoformat(), e.event_type, float(e.amount_signed)) for e in logs]
