"""Helpers for asset tests."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account.chart import chart_ref
from tessera.orm import Model

D = dt.date
FAR = "2100-12-31"


def make_asset(env: Any, *, validate: bool = True, **vals: Any) -> Model:
    company = env.company
    asset = env["account.asset"].create(
        {
            "name": vals.pop("name", "Test asset"),
            "company_id": company.ids[0],
            "account_asset_id": chart_ref(company, "fixed_assets").ids[0],
            "account_depreciation_id": chart_ref(company, "accumulated_depreciation").ids[0],
            "account_depreciation_expense_id": chart_ref(company, "depreciation").ids[0],
            "journal_id": chart_ref(company, "journal_misc").ids[0],
            **vals,
        }
    )
    if validate:
        asset.action_validate()
    return asset


def board(asset: Model, *, states: tuple[str, ...] = ("draft", "posted")) -> list[tuple[str, float]]:
    moves = asset.depreciation_move_ids.filtered(lambda m: m.state in states).sorted(lambda m: (m.date, m.ids[0]))
    return [(m.date.isoformat(), float(m.asset_depreciation_value)) for m in moves]


def entry_lines(move: Model) -> dict[str, float]:
    """Balance per account code of a journal entry."""
    out: dict[str, float] = {}
    for line in move.line_ids:
        code = line.account_id.code
        out[code] = round(out.get(code, 0.0) + float(line.balance), 2)
    return out


def month_ends(start: D, count: int) -> list[str]:
    """``count`` consecutive month ends from ``start``'s month (dates only; amounts stay hand-written)."""
    import calendar

    out = []
    year, month = start.year, start.month
    for _ in range(count):
        out.append(D(year, month, calendar.monthrange(year, month)[1]).isoformat())
        month += 1
        if month == 13:
            year, month = year + 1, 1
    return out
