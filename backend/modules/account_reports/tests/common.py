"""Helpers for report tests."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account_reports.engine import run_report
from tessera.orm import Model

D = dt.date


def acc(env: Any, code: str, company: Model | None = None) -> Model:
    company = company or env.company
    found = env["account.account"].search([("code", "=", code), ("company_id", "=", company.ids[0])], limit=1)
    assert found, f"no account {code}"
    return found


def entry(
    env: Any,
    day: dt.date,
    lines: list[tuple[Any, ...]],
    *,
    company: Model | None = None,
    post: bool = True,
    journal_type: str = "general",
    ref: str = "test",
) -> Model:
    """Post a journal entry: lines are (account code, balance) or (account code, balance, partner)."""
    company = company or env.company
    journal = env["account.journal"].search(
        [("type", "=", journal_type), ("company_id", "=", company.ids[0])], order="sequence, id", limit=1
    )
    move = env["account.move"].create(
        {
            "move_type": "entry",
            "journal_id": journal.ids[0],
            "company_id": company.ids[0],
            "date": day,
            "ref": ref,
            "line_ids": [
                (
                    "create",
                    {
                        "name": ref,
                        "account_id": acc(env, row[0], company).ids[0],
                        "balance": row[1],
                        "partner_id": row[2].ids[0] if len(row) > 2 and row[2] else None,
                    },
                )
                for row in lines
            ],
        }
    )
    if post:
        move.action_post()
    return move


def run(env: Any, report: str, **params: Any) -> dict[str, Any]:
    return run_report(env, {"report": report, **params})


def by_code(data: dict[str, Any], column: int = 0) -> dict[str, float]:
    return {line["code"]: line["values"][column] for line in data["lines"] if line.get("code")}


def row(data: dict[str, Any], row_id: str) -> dict[str, Any]:
    found = [line for line in data["lines"] if line["id"] == row_id]
    assert found, f"no row {row_id}: {[line['id'] for line in data['lines']]}"
    return found[0]


def domain_balance(env: Any, domain: Any) -> float:
    lines = env["account.move_line"].search(domain)
    return round(sum(float(x.balance) for x in lines), 2)
