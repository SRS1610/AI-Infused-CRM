"""Subscription KPIs and churn features, computed from the MRR history (``sale.subscription_log``).

* ``subscription_metrics(company, date_from, date_to)`` — MRR at both dates and its movements over the range
  (new, expansion, contraction, churn, reactivation; in the company currency, converted at each event's
  date), customer and revenue churn rates, plus the same per calendar month.
* ``churn_features(subscription)`` — the facts about one subscription the 2.6 churn prediction uses (tenure,
  MRR, payment failures, downgrades, pauses, customer messages…). No score is computed here.

Classification: *new* and *reactivation* events are their own movements; *upsell* is expansion and
*downsell* contraction; a renewal closes the old subscription (−old MRR) and starts the new one (+new MRR),
so the renewal movements of a range net to (new − old) and count as expansion when positive, contraction
when negative. *Churn* events are churn. Customer churn rate = subscriptions churned in the range /
subscriptions active at its start; revenue churn rate = churned MRR / MRR at the start.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from dateutil.relativedelta import relativedelta

from tessera.orm import Model


def _logs(company: Model, date_to: dt.date) -> list[Model]:
    env = company.env
    return list(
        env["sale.subscription_log"]
        .elevated("subscription metrics")
        .search([("company_id", "=", company.ids[0]), ("event_date", "<=", date_to)], order="event_date, id")
    )


def _active(logs: list[Model], day: dt.date) -> tuple[float, int]:
    """(MRR, number of subscriptions with a positive MRR) at the end of ``day``."""
    per_sub: dict[int, float] = defaultdict(float)
    for entry in logs:
        if entry.event_date <= day:
            per_sub[entry.subscription_id.ids[0]] += float(entry.amount_company or 0.0)
    total = sum(per_sub.values())
    return round(total, 2), sum(1 for v in per_sub.values() if v > 0.005)


def _movements(logs: list[Model], start: dt.date, end: dt.date) -> dict[str, Any]:
    moves = {"new": 0.0, "expansion": 0.0, "contraction": 0.0, "churn": 0.0, "reactivation": 0.0}
    renewal = 0.0
    churned: set[int] = set()
    for entry in logs:
        if not start <= entry.event_date <= end:
            continue
        amount = float(entry.amount_company or 0.0)
        kind = entry.event_type
        if kind == "new":
            moves["new"] += amount
        elif kind == "reactivation":
            moves["reactivation"] += amount
        elif kind == "upsell":
            moves["expansion"] += amount
        elif kind == "downsell":
            moves["contraction"] += amount
        elif kind == "renewal":
            renewal += amount
        elif kind == "churn":
            moves["churn"] += amount
            churned.add(entry.subscription_id.ids[0])
    if renewal > 0:
        moves["expansion"] += renewal
    else:
        moves["contraction"] += renewal
    out: dict[str, Any] = {k: round(v, 2) for k, v in moves.items()}
    out["net_change"] = round(sum(moves.values()), 2)
    out["churned_count"] = len(churned)
    return out


def _rates(mrr_start: float, active_start: int, movements: dict[str, Any]) -> dict[str, float | None]:
    return {
        "churn_rate": round(movements["churned_count"] / active_start, 4) if active_start else None,
        "revenue_churn_rate": round(-movements["churn"] / mrr_start, 4) if mrr_start else None,
        "net_revenue_retention": (
            round((mrr_start + movements["expansion"] + movements["contraction"] + movements["churn"]) / mrr_start, 4)
            if mrr_start
            else None
        ),
    }


def subscription_metrics(company: Model, date_from: dt.date, date_to: dt.date) -> dict[str, Any]:
    """MRR and its movements between ``date_from`` and ``date_to`` (both included) for ``company``."""
    logs = _logs(company, date_to)
    before = date_from - dt.timedelta(days=1)
    mrr_start, active_start = _active(logs, before)
    mrr_end, active_end = _active(logs, date_to)
    movements = _movements(logs, date_from, date_to)
    monthly = []
    month = date_from.replace(day=1)
    while month <= date_to:
        m_start = max(month, date_from)
        m_end = min(month + relativedelta(months=1) - dt.timedelta(days=1), date_to)
        m_mrr, m_active = _active(logs, m_start - dt.timedelta(days=1))
        m_moves = _movements(logs, m_start, m_end)
        monthly.append(
            {
                "month": month.strftime("%Y-%m"),
                "mrr_start": m_mrr,
                "mrr_end": _active(logs, m_end)[0],
                "active_start": m_active,
                **m_moves,
                **_rates(m_mrr, m_active, m_moves),
            }
        )
        month += relativedelta(months=1)
    return {
        "company": company.elevated().name,
        "currency": company.elevated().currency_id.name,
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "mrr_start": mrr_start,
        "mrr_end": mrr_end,
        "arr_end": round(mrr_end * 12, 2),
        "active_start": active_start,
        "active_end": active_end,
        **movements,
        **_rates(mrr_start, active_start, movements),
        "monthly": monthly,
    }


def churn_features(subscription: Model, day: dt.date | None = None) -> dict[str, Any]:
    """Facts about one subscription for churn prediction (no score here)."""
    sub = subscription.elevated("churn features")
    sub.ensure_one()
    env = sub.env
    day = day or env.today()
    logs = env["sale.subscription_log"].elevated().search([("subscription_id", "=", sub.ids[0])], order="event_date, id")
    moves = env["account.move"].elevated().search([("subscription_id", "=", sub.ids[0]), ("state", "=", "posted")])
    invoices = moves.filtered(lambda m: m.move_type == "out_invoice")
    unpaid = invoices.filtered(lambda m: m.payment_state in ("not_paid", "partial") and m.amount_residual > 0)
    overdue = unpaid.filtered(lambda m: (m.invoice_date_due or m.invoice_date) < day)
    paid = invoices.filtered(lambda m: m.payment_state in ("paid", "in_payment"))
    lines = sub._recurring_lines()
    start = sub.start_date
    end = sub.closed_on if sub.subscription_state in ("churned", "closed") and sub.closed_on else day
    tenure = (end - start).days if start else 0
    messages = env["chatter.message"].elevated().search_count(
        [("model", "=", "sale.quotation"), ("res_id", "=", sub.ids[0]), ("message_type", "=", "email")]
    )
    chain = 0
    previous = sub.renewal_of_id
    while previous and chain < 50:
        chain += 1
        previous = previous.renewal_of_id
    plan = sub.plan_id
    return {
        "subscription_id": sub.ids[0],
        "name": sub.name,
        "partner_id": sub.partner_id.commercial_partner_id.ids[0],
        "plan": plan.name if plan else None,
        "billing_period": f"{plan.billing_period_value} {plan.billing_period_unit}" if plan else None,
        "state": sub.subscription_state,
        "churned": sub.subscription_state == "churned",
        "close_reason": sub.close_reason_id.name if sub.close_reason_id else None,
        "tenure_days": tenure,
        "tenure_months": round(tenure / 30.4375, 1),
        "mrr": float(sub.recurring_monthly or 0.0),
        "mrr_company": float(sub.recurring_monthly_company or 0.0),
        "currency": sub.currency_id.name,
        "line_count": len(lines),
        "product_count": len(set(lines.mapped("product_id").ids)),
        "average_discount": round(sum(float(r.discount or 0) for r in lines) / len(lines), 2) if lines else 0.0,
        "fixed_term": bool(sub.end_date),
        "days_to_end": (sub.end_date - day).days if sub.end_date else None,
        "renewals": chain,
        "upsell_count": sum(1 for e in logs if e.event_type == "upsell"),
        "downsell_count": sum(1 for e in logs if e.event_type == "downsell"),
        "reactivation_count": sum(1 for e in logs if e.event_type == "reactivation"),
        "mrr_change_90d": round(
            sum(
                float(e.amount_signed or 0)
                for e in logs
                if e.event_type in ("upsell", "downsell") and (day - e.event_date).days <= 90
            ),
            2,
        ),
        "pause_count": int(sub.pause_count or 0),
        "paused": sub.subscription_state == "paused",
        "payment_failures": int(sub.payment_failure_count or 0),
        "has_payment_method": bool(sub.payment_token_id),
        "invoice_count": len(invoices),
        "credit_note_count": len(moves) - len(invoices),
        "unpaid_invoice_count": len(unpaid),
        "overdue_amount": round(sum(float(m.amount_residual) for m in overdue), 2),
        "days_since_last_payment": (
            (day - max(m.invoice_date for m in paid)).days if paid else None
        ),
        "customer_messages": messages,
    }
