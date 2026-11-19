"""Keep budget actuals current when the analytic items and journal items they sum change."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from tessera.orm import Model


def _refresh_analytic(env: Any, rows: list[tuple[int, int, Any]]) -> None:
    """rows: (company id, analytic account id, date)."""
    groups: dict[int, list[tuple[int, Any]]] = defaultdict(list)
    for company_id, account_id, day in rows:
        groups[company_id].append((account_id, day))
    lines = env["account.budget_line"]
    for company_id, items in groups.items():
        lines._refresh_for(
            company_id=company_id,
            start=min(d for _a, d in items),
            end=max(d for _a, d in items),
            analytic_ids={a for a, _d in items},
        )


class AnalyticLineBudget(Model):
    _extend = "account.analytic_line"

    def _budget_rows(self) -> list[tuple[int, int, Any]]:
        return [(line.company_id.ids[0], line.account_id.ids[0], line.date) for line in self.elevated()]

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        lines = super().create(vals_list)
        _refresh_analytic(self.env, lines._budget_rows())
        return lines

    def write(self, vals: dict[str, Any]) -> bool:
        before = self._budget_rows() if {"amount", "account_id", "date", "general_account_id"} & set(vals) else []
        result = super().write(vals)
        if before:
            _refresh_analytic(self.env, before + self._budget_rows())
        return result

    def unlink(self) -> bool:
        rows = self._budget_rows()
        result = super().unlink()
        _refresh_analytic(self.env, rows)
        return result


class MoveLineBudget(Model):
    _extend = "account.move_line"

    def write(self, vals: dict[str, Any]) -> bool:
        result = super().write(vals)
        if "parent_state" in vals and self._ids:
            groups: dict[int, list[Model]] = defaultdict(list)
            for line in self.elevated():
                if line.account_id and line.date:
                    groups[line.company_id.ids[0]].append(line)
            budget_lines = self.env["account.budget_line"]
            for company_id, items in groups.items():
                budget_lines._refresh_for(
                    company_id=company_id,
                    start=min(i.date for i in items),
                    end=max(i.date for i in items),
                    account_ids={i.account_id.ids[0] for i in items},
                )
        return result
