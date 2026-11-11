"""Acceptance 2-f (subscription half): the 20-scenario fixture.

Each scenario is a table row: the plan, the lines, the start (and end) date, a list of dated steps, and every
expected document (kind, date, untaxed, total, period), the final state, the next invoice date, the end date,
the MRR and the MRR history. Amounts follow the rules of ``periods.py`` / ``billing.py``:

* periods start on ``anchor + k x period`` (Jan 31 → Feb 28 → Mar 31 …) and end the day before the next start;
* a change or closure effective on day C prorates ``(period end - C + 1) / period days``, rounded per line to
  the cent; a fixed term ending inside a period invoices ``covered days / period days`` of it;
* MRR is the untaxed recurring amount per month (yearly ÷ 12, weekly x 52 / 12).

Document kinds: ``inv`` period invoice, ``pro`` mid-period change invoice, ``pcn`` mid-period change credit
note, ``cn`` unused days credit note (immediate close).

Steps: ``run`` (the daily job on that date), ``upsell`` (a change order confirmed that day: ``set`` changes
lines by index, ``add`` adds lines), ``pause`` / ``resume`` / ``reactivate``, ``close`` (``reason``, ``when``),
``card`` (the customer's saved card is replaced by one with this reference), ``renewal_price`` /
``renewal_confirm`` / ``renewal_reject`` (the renewal quotation), ``check`` (intermediate state).
"""

from __future__ import annotations

from typing import Any

import pytest

from modules.account.tests.common import make_tax
from modules.sale_subscription.tests.common import D, at, customer, docs, history, plan, recurring, run, token

pytestmark = pytest.mark.db

KINDS = {
    ("out_invoice", "period"): "inv",
    ("out_invoice", "proration"): "pro",
    ("out_refund", "proration"): "pcn",
    ("out_refund", "credit"): "cn",
}
A = {"product": "A", "qty": 1}


def d(text: str | None) -> Any:
    return D.fromisoformat(text) if text else None


SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "01-monthly-31st",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-01-31",
        "steps": [("2026-01-31", "run"), ("2026-02-27", "run"), ("2026-02-28", "run"), ("2026-03-31", "run"),
                  ("2026-04-30", "run"), ("2026-04-30", "run")],
        "invoices": [
            ("inv", "2026-01-31", 100.0, 100.0, "2026-01-31", "2026-02-27"),
            ("inv", "2026-02-28", 100.0, 100.0, "2026-02-28", "2026-03-30"),
            ("inv", "2026-03-31", 100.0, 100.0, "2026-03-31", "2026-04-29"),
            ("inv", "2026-04-30", 100.0, 100.0, "2026-04-30", "2026-05-30"),
        ],
        "state": "progress", "next": "2026-05-31", "mrr": 100.0,
        "log": [("2026-01-31", "new", 100.0)],
    },
    {
        "id": "02-yearly",
        "plan": {"unit": "year"},
        "products": {"A": 1200},
        "lines": [A],
        "start": "2026-03-01",
        "steps": [("2026-03-01", "run"), ("2026-06-01", "run"), ("2027-03-01", "run")],
        "invoices": [
            ("inv", "2026-03-01", 1200.0, 1200.0, "2026-03-01", "2027-02-28"),
            ("inv", "2027-03-01", 1200.0, 1200.0, "2027-03-01", "2028-02-29"),
        ],
        "state": "progress", "next": "2028-03-01", "mrr": 100.0,
        "log": [("2026-03-01", "new", 100.0)],
    },
    {
        "id": "03-weekly-catch-up",
        "plan": {"unit": "week"},
        "products": {"A": 30},
        "lines": [A],
        "start": "2026-02-02",
        "steps": [("2026-02-02", "run"), ("2026-02-16", "run")],
        "invoices": [
            ("inv", "2026-02-02", 30.0, 30.0, "2026-02-02", "2026-02-08"),
            ("inv", "2026-02-09", 30.0, 30.0, "2026-02-09", "2026-02-15"),
            ("inv", "2026-02-16", 30.0, 30.0, "2026-02-16", "2026-02-22"),
        ],
        "state": "progress", "next": "2026-02-23", "mrr": 130.0,
        "log": [("2026-02-02", "new", 130.0)],
    },
    {
        "id": "04-upsell-mid-period",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-03-01",
        "steps": [("2026-03-01", "run"), ("2026-03-16", "upsell", {"set": {0: {"quantity": 2}}}), ("2026-04-01", "run")],
        "invoices": [
            ("inv", "2026-03-01", 100.0, 100.0, "2026-03-01", "2026-03-31"),
            ("pro", "2026-03-16", 51.61, 51.61, "2026-03-16", "2026-03-31"),  # 100 x 16/31
            ("inv", "2026-04-01", 200.0, 200.0, "2026-04-01", "2026-04-30"),
        ],
        "state": "progress", "next": "2026-05-01", "mrr": 200.0,
        "log": [("2026-03-01", "new", 100.0), ("2026-03-16", "upsell", 100.0)],
    },
    {
        "id": "05-downgrade-credit",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [{"product": "A", "qty": 3}],
        "start": "2026-04-01",
        "steps": [("2026-04-01", "run"), ("2026-04-21", "upsell", {"set": {0: {"quantity": 1}}}), ("2026-05-01", "run")],
        "invoices": [
            ("inv", "2026-04-01", 300.0, 300.0, "2026-04-01", "2026-04-30"),
            ("pcn", "2026-04-21", 66.67, 66.67, "2026-04-21", "2026-04-30"),  # 200 x 10/30
            ("inv", "2026-05-01", 100.0, 100.0, "2026-05-01", "2026-05-31"),
        ],
        "state": "progress", "next": "2026-06-01", "mrr": 100.0,
        "log": [("2026-04-01", "new", 300.0), ("2026-04-21", "downsell", -200.0)],
        "residual_first": 233.33,  # the credit note is matched with the open April invoice
    },
    {
        "id": "06-two-changes-one-period",
        "plan": {"unit": "month"},
        "products": {"A": 100, "B": 50},
        "lines": [A],
        "start": "2026-05-01",
        "steps": [
            ("2026-05-01", "run"),
            ("2026-05-11", "upsell", {"add": [{"product": "B", "qty": 1}]}),
            ("2026-05-21", "upsell", {"set": {0: {"price_unit": 80.0}}}),
            ("2026-06-01", "run"),
        ],
        "invoices": [
            ("inv", "2026-05-01", 100.0, 100.0, "2026-05-01", "2026-05-31"),
            ("pro", "2026-05-11", 33.87, 33.87, "2026-05-11", "2026-05-31"),  # 50 x 21/31
            ("pcn", "2026-05-21", 7.10, 7.10, "2026-05-21", "2026-05-31"),  # 20 x 11/31
            ("inv", "2026-06-01", 130.0, 130.0, "2026-06-01", "2026-06-30"),
        ],
        "state": "progress", "next": "2026-07-01", "mrr": 130.0,
        "log": [("2026-05-01", "new", 100.0), ("2026-05-11", "upsell", 50.0), ("2026-05-21", "downsell", -20.0)],
    },
    {
        "id": "07-pause-resume-skips-periods",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-01-01",
        "steps": [
            ("2026-01-01", "run"), ("2026-01-20", "pause"), ("2026-02-01", "run"), ("2026-03-01", "run"),
            ("2026-03-01", "check", {"state": "paused", "next": "2026-02-01"}),
            ("2026-03-10", "resume"), ("2026-03-10", "check", {"state": "progress", "next": "2026-04-01"}),
            ("2026-03-10", "run"), ("2026-04-01", "run"),
        ],
        "invoices": [
            ("inv", "2026-01-01", 100.0, 100.0, "2026-01-01", "2026-01-31"),
            ("inv", "2026-04-01", 100.0, 100.0, "2026-04-01", "2026-04-30"),
        ],
        "state": "progress", "next": "2026-05-01", "mrr": 100.0,
        "log": [("2026-01-01", "new", 100.0)],
    },
    {
        "id": "08-close-now-credit",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-06-01",
        "steps": [("2026-06-01", "run"), ("2026-06-21", "close", {"reason": "price", "when": "now"}), ("2026-07-01", "run")],
        "invoices": [
            ("inv", "2026-06-01", 100.0, 100.0, "2026-06-01", "2026-06-30"),
            ("cn", "2026-06-21", 33.33, 33.33, "2026-06-21", "2026-06-30"),  # 100 x 10/30
        ],
        "state": "churned", "next": None, "end": "2026-06-20", "mrr": 0.0, "reason": "price",
        "log": [("2026-06-01", "new", 100.0), ("2026-06-21", "churn", -100.0)],
        "residual_first": 66.67,
    },
    {
        "id": "09-close-at-period-end",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-07-01",
        "steps": [
            ("2026-07-01", "run"), ("2026-07-10", "close", {"reason": "not_needed", "when": "period_end"}),
            ("2026-07-10", "check", {"state": "progress", "end": "2026-07-31"}),
            ("2026-07-31", "run"), ("2026-08-01", "run"),
        ],
        "invoices": [("inv", "2026-07-01", 100.0, 100.0, "2026-07-01", "2026-07-31")],
        "state": "churned", "next": None, "end": "2026-07-31", "mrr": 0.0, "reason": "not_needed",
        "log": [("2026-07-01", "new", 100.0), ("2026-08-01", "churn", -100.0)],
    },
    {
        "id": "10-fixed-term-renewal-accepted",
        "plan": {"unit": "month", "renewal_reminder_days": 15},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-01-01", "term_end": "2026-03-31",
        "steps": [
            ("2026-01-01", "run"), ("2026-02-01", "run"), ("2026-03-01", "run"), ("2026-03-15", "run"),
            ("2026-03-15", "check", {"renewals": 0}), ("2026-03-16", "run"), ("2026-03-17", "run"),
            ("2026-03-17", "check", {"renewals": 1}),
            ("2026-03-18", "renewal_price", {"price_unit": 110.0}), ("2026-03-20", "renewal_confirm"),
            ("2026-03-20", "check", {"renewal_state": "draft"}), ("2026-04-01", "run"),
        ],
        "invoices": [
            ("inv", "2026-01-01", 100.0, 100.0, "2026-01-01", "2026-01-31"),
            ("inv", "2026-02-01", 100.0, 100.0, "2026-02-01", "2026-02-28"),
            ("inv", "2026-03-01", 100.0, 100.0, "2026-03-01", "2026-03-31"),
        ],
        "state": "closed", "next": None, "end": "2026-03-31", "mrr": 0.0,
        "log": [("2026-01-01", "new", 100.0), ("2026-04-01", "renewal", -100.0)],
        "renewal": {
            "start": "2026-04-01", "end": "2026-06-30", "state": "progress", "next": "2026-05-01", "mrr": 110.0,
            "invoices": [("inv", "2026-04-01", 110.0, 110.0, "2026-04-01", "2026-04-30")],
            "log": [("2026-04-01", "renewal", 110.0)],
        },
    },
    {
        "id": "11-renewal-refused-churn",
        "plan": {"unit": "month", "renewal_reminder_days": 10},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-02-01", "term_end": "2026-04-30",
        "steps": [
            ("2026-02-01", "run"), ("2026-03-01", "run"), ("2026-04-01", "run"), ("2026-04-20", "run"),
            ("2026-04-22", "renewal_reject"), ("2026-04-25", "run"), ("2026-04-25", "check", {"renewals": 1}),
            ("2026-05-01", "run"),
        ],
        "invoices": [
            ("inv", "2026-02-01", 100.0, 100.0, "2026-02-01", "2026-02-28"),
            ("inv", "2026-03-01", 100.0, 100.0, "2026-03-01", "2026-03-31"),
            ("inv", "2026-04-01", 100.0, 100.0, "2026-04-01", "2026-04-30"),
        ],
        "state": "churned", "next": None, "end": "2026-04-30", "mrr": 0.0, "reason": "not_renewed",
        "log": [("2026-02-01", "new", 100.0), ("2026-05-01", "churn", -100.0)],
    },
    {
        "id": "12-token-charge-success",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-08-01", "token": "good-card",
        "steps": [("2026-08-01", "run"), ("2026-09-01", "run")],
        "invoices": [
            ("inv", "2026-08-01", 100.0, 100.0, "2026-08-01", "2026-08-31", "paid"),
            ("inv", "2026-09-01", 100.0, 100.0, "2026-09-01", "2026-09-30", "paid"),
        ],
        "state": "progress", "next": "2026-10-01", "mrr": 100.0, "failures": 0,
        "log": [("2026-08-01", "new", 100.0)],
    },
    {
        "id": "13-charge-failure-then-retry-success",
        "plan": {"unit": "month", "payment_retry_days": 3, "auto_close_limit": 15},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-09-01", "token": "fail-card",
        "steps": [
            ("2026-09-01", "run"), ("2026-09-01", "check", {"failures": 1, "paid": False}),
            ("2026-09-02", "run"), ("2026-09-02", "check", {"failures": 1}),
            ("2026-09-03", "card", {"ref": "good-card"}), ("2026-09-04", "run"),
        ],
        "invoices": [("inv", "2026-09-01", 100.0, 100.0, "2026-09-01", "2026-09-30", "paid")],
        "state": "progress", "next": "2026-10-01", "mrr": 100.0, "failures": 1,
        "log": [("2026-09-01", "new", 100.0)],
    },
    {
        "id": "14-failures-until-auto-close",
        "plan": {"unit": "month", "payment_retry_days": 3, "auto_close_limit": 10},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-10-01", "token": "fail-card",
        "steps": [("2026-10-01", "run"), ("2026-10-04", "run"), ("2026-10-07", "run"), ("2026-10-10", "run"),
                  ("2026-10-10", "check", {"state": "progress"}), ("2026-10-11", "run")],
        "invoices": [("inv", "2026-10-01", 100.0, 100.0, "2026-10-01", "2026-10-31", "not_paid")],
        "state": "churned", "next": None, "end": "2026-10-10", "mrr": 0.0, "failures": 4,
        "reason": "payment_failure",
        "log": [("2026-10-01", "new", 100.0), ("2026-10-11", "churn", -100.0)],
    },
    {
        "id": "15-reactivation",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-01-01",
        "steps": [("2026-01-01", "run"), ("2026-01-11", "close", {"reason": "quality", "when": "now"}),
                  ("2026-02-15", "reactivate"), ("2026-02-15", "run")],
        "invoices": [
            ("inv", "2026-01-01", 100.0, 100.0, "2026-01-01", "2026-01-31"),
            ("cn", "2026-01-11", 67.74, 67.74, "2026-01-11", "2026-01-31"),  # 100 x 21/31
            ("inv", "2026-02-15", 100.0, 100.0, "2026-02-15", "2026-03-14"),
        ],
        "state": "progress", "next": "2026-03-15", "mrr": 100.0,
        "log": [("2026-01-01", "new", 100.0), ("2026-01-11", "churn", -100.0), ("2026-02-15", "reactivation", 100.0)],
    },
    {
        "id": "16-multi-currency-pricelist",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [{"product": "A", "qty": 1, "price_unit": 90.0}],
        "start": "2026-03-01", "french": True,
        "steps": [("2026-03-01", "run")],
        "invoices": [("inv", "2026-03-01", 90.0, 90.0, "2026-03-01", "2026-03-31")],
        "state": "progress", "next": "2026-04-01", "mrr": 90.0, "currency": "EUR",
        "log": [("2026-03-01", "new", 90.0)], "log_company": [100.0],  # 90 EUR at 0.9 EUR per USD
    },
    {
        "id": "17-tax-included-prices",
        "plan": {"unit": "month"},
        "products": {"A": 120},
        "tax_included": 20.0,
        "lines": [A],
        "start": "2026-05-01",
        "steps": [("2026-05-01", "run"), ("2026-05-16", "upsell", {"set": {0: {"quantity": 2}}}), ("2026-06-01", "run")],
        "invoices": [
            ("inv", "2026-05-01", 100.0, 120.0, "2026-05-01", "2026-05-31"),
            ("pro", "2026-05-16", 51.62, 61.94, "2026-05-16", "2026-05-31"),  # 120 TTC x 16/31 = 61.94
            ("inv", "2026-06-01", 200.0, 240.0, "2026-06-01", "2026-06-30"),
        ],
        "state": "progress", "next": "2026-07-01", "mrr": 200.0,
        "log": [("2026-05-01", "new", 100.0), ("2026-05-16", "upsell", 100.0)],
    },
    {
        "id": "18-quantity-change-yearly",
        "plan": {"unit": "year"},
        "products": {"A": 1200},
        "lines": [{"product": "A", "qty": 5}],
        "start": "2026-01-01",
        "steps": [("2026-01-01", "run"), ("2026-07-02", "upsell", {"set": {0: {"quantity": 8}}}), ("2027-01-01", "run")],
        "invoices": [
            ("inv", "2026-01-01", 6000.0, 6000.0, "2026-01-01", "2026-12-31"),
            ("pro", "2026-07-02", 1804.93, 1804.93, "2026-07-02", "2026-12-31"),  # 3600 x 183/365
            ("inv", "2027-01-01", 9600.0, 9600.0, "2027-01-01", "2027-12-31"),
        ],
        "state": "progress", "next": "2028-01-01", "mrr": 800.0,
        "log": [("2026-01-01", "new", 500.0), ("2026-07-02", "upsell", 300.0)],
    },
    {
        "id": "19-discount-line",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [{"product": "A", "qty": 2, "discount": 25.0}],
        "start": "2026-02-01",
        "steps": [("2026-02-01", "run"), ("2026-02-15", "upsell", {"set": {0: {"discount": 10.0}}}), ("2026-03-01", "run")],
        "invoices": [
            ("inv", "2026-02-01", 150.0, 150.0, "2026-02-01", "2026-02-28"),
            ("pro", "2026-02-15", 15.0, 15.0, "2026-02-15", "2026-02-28"),  # (180 - 150) x 14/28
            ("inv", "2026-03-01", 180.0, 180.0, "2026-03-01", "2026-03-31"),
        ],
        "state": "progress", "next": "2026-04-01", "mrr": 180.0, "first_discount": 25.0,
        "log": [("2026-02-01", "new", 150.0), ("2026-02-15", "upsell", 30.0)],
    },
    {
        "id": "20-end-date-mid-period",
        "plan": {"unit": "month"},
        "products": {"A": 100},
        "lines": [A],
        "start": "2026-01-01", "term_end": "2026-03-15",
        "steps": [("2026-01-01", "run"), ("2026-02-01", "run"), ("2026-03-01", "run"), ("2026-03-15", "run"),
                  ("2026-03-16", "run")],
        "invoices": [
            ("inv", "2026-01-01", 100.0, 100.0, "2026-01-01", "2026-01-31"),
            ("inv", "2026-02-01", 100.0, 100.0, "2026-02-01", "2026-02-28"),
            ("inv", "2026-03-01", 48.39, 48.39, "2026-03-01", "2026-03-15"),  # 100 x 15/31
        ],
        "state": "churned", "next": None, "end": "2026-03-15", "mrr": 0.0, "reason": "not_renewed",
        "log": [("2026-01-01", "new", 100.0), ("2026-03-16", "churn", -100.0)],
    },
]  # fmt: skip


def _setup(env: Any, sc: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
    taxes = None
    if sc.get("tax_included"):
        taxes = make_tax(env, f"Test VAT {sc['tax_included']:g}% incl", sc["tax_included"], price_include=True)
    products = {key: recurring(env, f"{sc['id']} {key}", price, taxes=taxes) for key, price in sc["products"].items()}
    the_plan = plan(
        env,
        sc["plan"]["unit"],
        sc["plan"].get("value", 1),
        **{k: v for k, v in sc["plan"].items() if k not in ("unit", "value")},
    )
    vals: dict[str, Any] = {}
    if sc.get("french"):
        eur = env["core.currency"].search([("name", "=", "EUR")], limit=1)
        env["core.currency_rate"].elevated().create(
            {"currency_id": eur.ids[0], "company_id": env.company.ids[0], "date": D(2026, 3, 1), "rate": 0.9}
        )
        partner = customer(env, f"{sc['id']} Lyon", country_id=env.ref("core.country_fr").ids[0])
    else:
        partner = customer(env, f"{sc['id']} customer")
    if sc.get("token"):
        vals["payment_token_id"] = token(env, partner, sc["token"]).ids[0]
    lines = []
    for spec in sc["lines"]:
        line = {"product_id": products[spec["product"]].ids[0], "quantity": spec["qty"]}
        for key in ("price_unit", "discount"):
            if key in spec:
                line[key] = spec[key]
        lines.append(line)
    start = d(sc["start"])
    e = at(env, start)
    sub = e["sale.quotation"].create(
        {
            "partner_id": partner.ids[0],
            "plan_id": the_plan.ids[0],
            "start_date": start,
            "end_date": d(sc.get("term_end")),
            "date_order": f"{sc['start']}T09:00:00",
            "validity_date": start,
            "payment_term_id": env.ref("account.term_immediate").ids[0],
            "line_ids": [("create", line) for line in lines],
            **vals,
        }
    )
    sub.action_confirm()
    return sub, {"products": products, "partner": partner}


def _upsell(env: Any, sub: Any, day: Any, args: dict[str, Any], ctx: dict[str, Any]) -> None:
    e = at(env, day)
    info = sub.with_env(e).action_upsell()
    change = e["sale.quotation"].browse([info["upsell_id"]])
    lines = change.line_ids.sorted(lambda r: (r.sequence, r.ids[0]))
    for index, vals in args.get("set", {}).items():
        lines[index].write(vals)
    for spec in args.get("add", []):
        product = ctx["products"][spec["product"]]
        change.write({"line_ids": [("create", {"product_id": product.ids[0], "quantity": spec["qty"]})]})
    change.action_confirm()
    assert change.state == "accepted" and change.upsell_of_id == sub


def _renewal(sub: Any) -> Any:
    return sub.renewal_ids.sorted(lambda r: r.ids[0])[-1:]


def _doc_rows(sub: Any) -> list[tuple[Any, ...]]:
    rows = []
    for move in docs(sub):
        state = "paid" if move.payment_state == "in_payment" else move.payment_state
        rows.append(
            (
                KINDS[(move.move_type, move.subscription_kind)],
                move.invoice_date.isoformat(),
                float(move.amount_untaxed),
                float(move.amount_total),
                move.subscription_period_start.isoformat(),
                move.subscription_period_end.isoformat(),
                state,
            )
        )
    return rows


def _compare_docs(sub: Any, expected: list[tuple[Any, ...]]) -> None:
    rows = _doc_rows(sub)
    got = [row[: len(exp)] for row, exp in zip(rows, expected, strict=False)]
    assert got == [tuple(e) for e in expected], rows
    assert len(rows) == len(expected), rows


def _check(sub: Any, expect: dict[str, Any]) -> None:
    if "state" in expect:
        assert sub.subscription_state == expect["state"]
    if "next" in expect:
        assert sub.next_invoice_date == d(expect["next"])
    if "end" in expect:
        assert sub.end_date == d(expect["end"])
    if "failures" in expect:
        assert sub.payment_failure_count == expect["failures"]
    if "renewals" in expect:
        assert len(sub.renewal_ids) == expect["renewals"]
    if "renewal_state" in expect:
        assert _renewal(sub).subscription_state == expect["renewal_state"]
    if "paid" in expect:
        paid = all(m.payment_state in ("paid", "in_payment") for m in docs(sub) if m.move_type == "out_invoice")
        assert paid == expect["paid"]


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_subscription_scenario(env: Any, sc: dict[str, Any]) -> None:
    sub, ctx = _setup(env, sc)
    for step in sc["steps"]:
        day, action = d(step[0]), step[1]
        args = step[2] if len(step) > 2 else {}
        e = at(env, day)
        mine = sub.with_env(e)
        if action == "run":
            run(e, day)
        elif action == "upsell":
            _upsell(env, sub, day, args, ctx)
        elif action == "pause":
            mine.action_pause()
        elif action == "resume":
            mine.action_resume()
        elif action == "reactivate":
            mine.action_reactivate()
        elif action == "close":
            mine.action_close(reason=args["reason"], when=args["when"])
        elif action == "card":
            sub.payment_token_id.write({"provider_ref": args["ref"]})
        elif action == "renewal_price":
            _renewal(sub).line_ids[:1].write(args)
        elif action == "renewal_confirm":
            _renewal(sub).with_env(e).action_confirm()
        elif action == "renewal_reject":
            _renewal(sub).with_env(e).action_reject("Too expensive")
        elif action == "check":
            _check(sub, args)
        else:  # pragma: no cover - a typo in the table
            raise AssertionError(action)
    _compare_docs(sub, sc["invoices"])
    _check(sub, {k: sc[k] for k in ("state", "next", "end", "failures") if k in sc})
    assert float(sub.recurring_monthly) == sc["mrr"]
    assert history(sub) == sc["log"]
    if sc.get("reason"):
        assert sub.close_reason_id == env.ref(f"sale_subscription.close_reason_{sc['reason']}")
    if sc.get("currency"):
        assert sub.currency_id.name == sc["currency"]
        assert {m.currency_id.name for m in docs(sub)} == {sc["currency"]}
    if sc.get("log_company"):
        logs = sub.subscription_log_ids.sorted(lambda r: (r.event_date, r.ids[0]))
        assert [float(e.amount_company) for e in logs] == sc["log_company"]
    if "residual_first" in sc:
        assert float(docs(sub)[0].amount_residual) == sc["residual_first"]
    if "first_discount" in sc:
        lines = docs(sub)[0].invoice_line_ids.filtered(lambda r: r.display_type == "product")
        assert {float(r.discount) for r in lines} == {sc["first_discount"]}
    if sc.get("renewal"):
        exp = sc["renewal"]
        renewal = _renewal(sub)
        assert (renewal.start_date, renewal.end_date) == (d(exp["start"]), d(exp["end"]))
        assert renewal.renewal_of_id == sub and renewal.subscription_state == exp["state"]
        assert renewal.next_invoice_date == d(exp["next"])
        assert float(renewal.recurring_monthly) == exp["mrr"]
        _compare_docs(renewal, exp["invoices"])
        assert history(renewal) == exp["log"]
    # the history always adds up to the current MRR
    assert round(sum(e[2] for e in history(sub)), 2) == sc["mrr"]


def test_twenty_scenarios() -> None:
    assert len(SCENARIOS) == 20 and len({s["id"] for s in SCENARIOS}) == 20
