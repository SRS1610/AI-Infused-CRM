"""Asset services: the depreciation job and the structured data the 2.6 AI features read.

* ``asset_profile(asset)`` — method, period, values and the whole board of an asset (for "depreciation
  anomaly checks" and explanations).
* ``board_checks(asset)`` — deterministic consistency checks of a board (the baseline the anomaly feature
  builds on): amounts that do not add up, negative entries, drafts left behind posted ones, overdue drafts.
* ``asset_model_candidates(env, company)`` — the asset models a bill line may be classified into ("asset
  classification suggestions" must pick one of these).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.jobs import job
from tessera.logging import get_logger
from tessera.orm import Model

log = get_logger(__name__)


def _account(rec: Model) -> dict[str, Any] | None:
    return {"id": rec.ids[0], "code": rec.code, "name": rec.name} if rec else None


def asset_profile(asset: Model) -> dict[str, Any]:
    asset.ensure_one()
    a = asset.elevated("asset profile")
    moves = a._depreciation_moves()
    return {
        "asset_id": a.ids[0],
        "name": a.name,
        "state": a.state,
        "asset_type": a.asset_type,
        "company": a.company_id.name,
        "currency": a.company_id.currency_id.name,
        "model": a.model_id.name if a.model_id else None,
        "method": a.method,
        "method_number": a.method_number,
        "method_period_months": int(a.method_period or 12),
        "method_progress_factor": float(a.method_progress_factor or 0) if a.method != "linear" else None,
        "prorata_computation_type": a.prorata_computation_type,
        "acquisition_date": a.acquisition_date.isoformat() if a.acquisition_date else None,
        "prorata_date": a.prorata_date.isoformat() if a.prorata_date else None,
        "original_value": float(a.original_value or 0),
        "salvage_value": float(a.salvage_value or 0),
        "depreciated_value": float(a.depreciated_value or 0),
        "book_value": float(a.book_value or 0),
        "value_residual": float(a.value_residual or 0),
        "life_units": float(a.life_units or 0),
        "accounts": {
            "asset": _account(a.account_asset_id),
            "accumulated_depreciation": _account(a.account_depreciation_id),
            "expense": _account(a.account_depreciation_expense_id),
        },
        "gross_increases": [
            {"asset_id": c.ids[0], "original_value": float(c.original_value or 0)} for c in a.children_ids
        ],
        "disposal_date": a.disposal_date.isoformat() if a.disposal_date else None,
        "board": [
            {
                "move_id": m.ids[0],
                "date": m.date.isoformat(),
                "state": m.state,
                "amount": float(m.asset_depreciation_value or 0),
                "units": float(m.asset_depreciation_units or 0),
                "cumulative": float(m.asset_depreciated_value or 0),
                "remaining": float(m.asset_remaining_value or 0),
                "value_change": bool(m.asset_value_change),
            }
            for m in moves
        ],
    }


def board_checks(asset: Model, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Consistency problems of an asset's board (empty list when it is sound)."""
    profile = asset_profile(asset)
    today = today or asset.env.today()
    issues: list[dict[str, Any]] = []
    entries = [e for e in profile["board"] if e["state"] != "cancel"]
    depreciable = profile["original_value"] - profile["salvage_value"]
    total = round(sum(e["amount"] for e in entries), 2)
    if profile["state"] in ("open", "draft") and entries and abs(total - depreciable) > 0.005:
        issues.append({"code": "board_total", "expected": round(depreciable, 2), "actual": total})
    for e in entries:
        if e["amount"] < 0:
            issues.append({"code": "negative_amount", "move_id": e["move_id"], "amount": e["amount"]})
        if e["state"] == "draft" and dt.date.fromisoformat(e["date"]) < today and profile["state"] == "open":
            issues.append({"code": "overdue_draft", "move_id": e["move_id"], "date": e["date"]})
    posted_dates = [e["date"] for e in entries if e["state"] == "posted"]
    for e in entries:
        if e["state"] == "draft" and posted_dates and e["date"] < max(posted_dates):
            issues.append({"code": "draft_before_posted", "move_id": e["move_id"], "date": e["date"]})
    if profile["book_value"] < profile["salvage_value"] - 0.005 and profile["state"] != "close":
        issues.append({"code": "below_salvage", "book_value": profile["book_value"]})
    return issues


def asset_model_candidates(env: Any, company: Model | None = None) -> list[dict[str, Any]]:
    company = company or env.company
    models = env["account.asset"].search([("state", "=", "model"), ("company_id", "=", company.ids[0])])
    return [
        {
            "model_id": m.ids[0],
            "name": m.name,
            "method": m.method,
            "method_number": m.method_number,
            "method_period_months": int(m.method_period or 12),
            "asset_account": _account(m.account_asset_id),
        }
        for m in models
    ]


def post_due_depreciation(env: Any, day: dt.date | None = None) -> int:
    """Post every draft depreciation entry dated up to ``day`` of running assets, in date order."""
    day = day or env.today()
    moves = (
        env["account.move"]
        .elevated("depreciation posting")
        .search([("asset_id", "!=", None), ("state", "=", "draft"), ("date", "<=", day)], order="date, id")
    )
    assets = moves.mapped("asset_id").filtered(lambda a: a.state == "open")
    count = 0
    for asset in assets:
        try:
            with env.cr.savepoint():
                count += asset.action_post_due(day.isoformat())
        except Exception as exc:  # one broken asset must not block the others
            log.warning("depreciation posting failed", asset=asset.ids[0], error=str(exc))
    return count


@job("account_asset.post_depreciation", queue="default", max_attempts=1, as_system=True)
def post_depreciation_job(env: Any, date: str | None = None) -> dict[str, Any]:
    day = dt.date.fromisoformat(date) if date else env.today()
    return {"date": day.isoformat(), "posted": post_due_depreciation(env, day)}
