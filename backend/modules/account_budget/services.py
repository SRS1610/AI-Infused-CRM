"""Budget services: the structured variance report (the only numbers the 2.6 "plain-language variance
explanations" may use) and the daily refresh job."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account_budget.models.budget import as_of_date
from tessera.jobs import job
from tessera.orm import Model


def _round(value: float) -> float:
    return round(float(value), 2)


def variance_report(budget: Model, as_of: dt.date | str | None = None) -> dict[str, Any]:
    """Budget vs actual of ``budget`` as of a date (default: today), recomputed live from the ledger.

    Every amount is in the company currency. ``favourable`` is True when the line is under budget (expense)
    or above target (revenue). Totals are per line type, so costs and revenue are never netted silently.
    """
    budget.ensure_one()
    if isinstance(as_of, str):
        as_of = dt.date.fromisoformat(as_of)
    day = as_of or as_of_date(budget.env)
    budget = budget.with_context(budget_date=day)
    rec = budget.elevated("budget variance report")
    lines: list[dict[str, Any]] = []
    totals: dict[str, dict[str, float]] = {}
    for line in rec.line_ids.sorted(lambda r: (r.sequence or 0, r.date_from, r.ids[0])):
        values = line._amounts(day)
        planned = float(line.planned_amount or 0)
        practical = values["practical_amount"]
        variance = values["variance"]
        favourable = variance <= 0 if line.line_type == "expense" else variance >= 0
        lines.append(
            {
                "line_id": line.ids[0],
                "name": line.name or (line.analytic_account_id.name if line.analytic_account_id else ""),
                "line_type": line.line_type,
                "analytic_account": (
                    {
                        "id": line.analytic_account_id.ids[0],
                        "code": line.analytic_account_id.code,
                        "name": line.analytic_account_id.name,
                    }
                    if line.analytic_account_id
                    else None
                ),
                "accounts": [{"id": a.ids[0], "code": a.code, "name": a.name} for a in line.account_ids],
                "account_type": line.account_type,
                "date_from": line.date_from.isoformat(),
                "date_to": line.date_to.isoformat(),
                "planned": _round(planned),
                "actual": _round(practical),
                "theoretical": _round(values["theoretical_amount"]),
                "variance": _round(variance),
                "variance_pct": _round(variance / planned * 100) if planned else None,
                "achievement_pct": _round(values["achievement"]),
                "elapsed_share": round(values["elapsed_share"], 4),
                "favourable": favourable,
            }
        )
        slot = totals.setdefault(line.line_type, {"planned": 0.0, "actual": 0.0, "theoretical": 0.0, "variance": 0.0})
        slot["planned"] = _round(slot["planned"] + planned)
        slot["actual"] = _round(slot["actual"] + practical)
        slot["theoretical"] = _round(slot["theoretical"] + values["theoretical_amount"])
        slot["variance"] = _round(slot["variance"] + variance)
    net_planned = totals.get("revenue", {}).get("planned", 0.0) - totals.get("expense", {}).get("planned", 0.0)
    net_actual = totals.get("revenue", {}).get("actual", 0.0) - totals.get("expense", {}).get("actual", 0.0)
    return {
        "budget_id": rec.ids[0],
        "name": rec.name,
        "state": rec.state,
        "company": rec.company_id.name,
        "currency": rec.company_id.currency_id.name,
        "date_from": rec.date_from.isoformat(),
        "date_to": rec.date_to.isoformat(),
        "as_of": day.isoformat(),
        "lines": lines,
        "totals": totals,
        "net": {
            "planned": _round(net_planned),
            "actual": _round(net_actual),
            "variance": _round(net_actual - net_planned),
        },
    }


@job("account_budget.refresh", queue="default", max_attempts=1, as_system=True)
def refresh_budgets_job(env: Any) -> dict[str, Any]:
    """Recompute actual and theoretical amounts of every draft or confirmed budget (daily)."""
    budgets = env["account.budget"].elevated().search([("state", "in", ["draft", "confirmed"])])
    budgets.action_refresh()
    return {"budgets": len(budgets)}
