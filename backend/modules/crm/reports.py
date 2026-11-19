"""Pipeline analytics. Every figure is computed with read_group so it reconciles with the pivot views."""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tessera.orm.environment import Environment


def _key(value: Any) -> Any:
    return value.get("id") if isinstance(value, dict) else value


def _label(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("display_name") or "—")
    return "—" if value is None else str(value)


def _range(date_from: dt.date | None, date_to: dt.date | None) -> list[Any]:
    domain: list[Any] = []
    if date_from:
        domain.append(("date_closed", ">=", date_from.isoformat()))
    if date_to:
        domain.append(("date_closed", "<", (date_to + dt.timedelta(days=1)).isoformat()))
    return domain


def win_rate(
    env: Environment, group_by: str = "user_id", date_from: dt.date | None = None, date_to: dt.date | None = None
) -> list[dict[str, Any]]:
    """won / (won + lost) per salesperson or team, over opportunities closed in the period."""
    leads = env["crm.lead"].with_context(active_test=False)
    period = _range(date_from, date_to)
    won = leads.read_group(
        [("type", "=", "opportunity"), ("is_won", "=", True), *period], ["expected_revenue:sum"], [group_by]
    )
    lost = leads.read_group([("type", "=", "opportunity"), ("lost", "=", True), *period], [], [group_by])
    rows: dict[Any, dict[str, Any]] = {}
    for g in won:
        rows.setdefault(
            _key(g[group_by]),
            {"key": _key(g[group_by]), "label": _label(g[group_by]), "won": 0, "lost": 0, "won_revenue": 0.0},
        )
        rows[_key(g[group_by])].update({"won": g["__count"], "won_revenue": g["expected_revenue:sum"] or 0.0})
    for g in lost:
        rows.setdefault(
            _key(g[group_by]),
            {"key": _key(g[group_by]), "label": _label(g[group_by]), "won": 0, "lost": 0, "won_revenue": 0.0},
        )
        rows[_key(g[group_by])]["lost"] = g["__count"]
    out = []
    for row in rows.values():
        closed = row["won"] + row["lost"]
        row["win_rate"] = round(100.0 * row["won"] / closed, 2) if closed else 0.0
        out.append(row)
    return sorted(out, key=lambda r: (-r["win_rate"], r["label"]))


def sales_velocity(
    env: Environment, date_from: dt.date | None = None, date_to: dt.date | None = None
) -> dict[str, Any]:
    """velocity = open opportunities x average won deal x win rate / average sales cycle (days), per day."""
    leads = env["crm.lead"].with_context(active_test=False)
    period = _range(date_from, date_to)
    open_count = leads.search_count([("type", "=", "opportunity"), ("active", "=", True), ("is_won", "=", False)])
    won = leads.search([("type", "=", "opportunity"), ("is_won", "=", True), *period])
    lost_count = leads.search_count([("type", "=", "opportunity"), ("lost", "=", True), *period])
    won_count = len(won)
    avg_deal = (sum(won.mapped("expected_revenue")) / won_count) if won_count else 0.0
    rate = won_count / (won_count + lost_count) if (won_count + lost_count) else 0.0
    cycles = [(w.date_closed - w.create_date).total_seconds() / 86400 for w in won if w.date_closed and w.create_date]
    avg_cycle = (sum(cycles) / len(cycles)) if cycles else 0.0
    velocity = (open_count * avg_deal * rate / avg_cycle) if avg_cycle > 0 else 0.0
    return {
        "open_opportunities": open_count,
        "average_deal": round(avg_deal, 2),
        "win_rate": round(rate * 100, 2),
        "average_cycle_days": round(avg_cycle, 2),
        "velocity_per_day": round(velocity, 2),
    }


def funnel(env: Environment, team_id: int | None = None) -> list[dict[str, Any]]:
    """Open + won opportunities per stage (in stage order), with the count that reached each stage
    (an opportunity in a later stage has passed the earlier ones)."""
    domain: list[Any] = [("type", "=", "opportunity"), ("active", "=", True)]
    if team_id:
        domain.append(("team_id", "=", team_id))
    groups = env["crm.lead"].read_group(domain, ["expected_revenue:sum"], ["stage_id"])
    stages = env["crm.stage"].search([], order="sequence, id")
    by_stage = {_key(g["stage_id"]): g for g in groups}
    rows = [
        {
            "stage_id": s.ids[0],
            "stage": s.name,
            "count": by_stage.get(s.ids[0], {}).get("__count", 0),
            "revenue": by_stage.get(s.ids[0], {}).get("expected_revenue:sum") or 0.0,
        }
        for s in stages
    ]
    reached = 0
    for row in reversed(rows):
        reached += row["count"]
        row["reached"] = reached
    top = rows[0]["reached"] if rows else 0
    for row in rows:
        row["conversion"] = round(100.0 * row["reached"] / top, 2) if top else 0.0
    return rows


def forecast(env: Environment, team_id: int | None = None) -> list[dict[str, Any]]:
    """Open opportunities by expected closing month: pipeline total and probability-weighted revenue."""
    domain: list[Any] = [("type", "=", "opportunity"), ("active", "=", True), ("is_won", "=", False)]
    if team_id:
        domain.append(("team_id", "=", team_id))
    groups = env["crm.lead"].read_group(
        domain, ["expected_revenue:sum", "prorated_revenue:sum"], ["date_deadline:month"]
    )
    return [
        {
            "month": g["date_deadline:month"],
            "count": g["__count"],
            "pipeline": round(g["expected_revenue:sum"] or 0.0, 2),
            "weighted": round(g["prorated_revenue:sum"] or 0.0, 2),
        }
        for g in groups
    ]
