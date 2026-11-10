"""Tax periods, the tax report's tie to the ledger and the tax closing entry.

* The tax return report (``tax``) resolves to the report of the company's chart template. Every tax report
  defines the lines ``TAX_DUE``, ``TAX_DEDUCTIBLE`` and ``NET`` (= due - deductible).
* Tie to the ledger: ``NET`` equals minus the balance movement of the tax accounts (the accounts on the
  taxes' distribution lines) over the period, tax closing entries excluded.
* Closing: one entry dated on the period's last day moves the balance of each tax account, per tax group,
  to the group's tax payable account (when the group owes tax) or its tax receivable account (credit).
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from modules.account_reports.engine import add_months, month_end, run_report
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model

PERIOD_MONTHS = {"monthly": 1, "quarterly": 3}


def period_of(company: Model, day: dt.date) -> tuple[dt.date, dt.date]:
    """The tax period (monthly or quarterly, calendar aligned) containing ``day``."""
    months = PERIOD_MONTHS.get(company.elevated().account_tax_periodicity or "quarterly", 3)
    first_month = ((day.month - 1) // months) * months + 1
    start = dt.date(day.year, first_month, 1)
    return start, month_end(add_months(start, months - 1))


def tax_report(env: Any, company: Model, date_from: dt.date, date_to: dt.date) -> dict[str, Any]:
    return run_report(
        env,
        {"report": "tax", "date_from": date_from, "date_to": date_to, "company_ids": company.ids, "hide_zero": False},
    )


def report_totals(data: dict[str, Any]) -> dict[str, float]:
    """``{"TAX_DUE": …, "TAX_DEDUCTIBLE": …, "NET": …}`` of a tax report run (first column)."""
    out = {"TAX_DUE": 0.0, "TAX_DEDUCTIBLE": 0.0, "NET": 0.0}
    for line in data["lines"]:
        if line.get("code") in out:
            out[line["code"]] = float(line["values"][0] or 0)
    return out


def tax_account_ids(env: Any, company: Model) -> list[int]:
    rows = env.cr.fetchall(
        "SELECT DISTINCT r.account_id FROM account_tax_repartition_line r JOIN account_tax t ON t.id = r.tax_id "
        "WHERE t.company_id = :cid AND r.repartition_type = 'tax' AND r.account_id IS NOT NULL",
        {"cid": company.ids[0]},
    )
    return [int(r[0]) for r in rows]


def closing_move_ids(env: Any, company: Model) -> list[int]:
    rows = env.cr.fetchall(
        "SELECT closing_move_id FROM account_tax_return WHERE company_id = :cid AND closing_move_id IS NOT NULL",
        {"cid": company.ids[0]},
    )
    return [int(r[0]) for r in rows]


def ledger_check(env: Any, company: Model, date_from: dt.date, date_to: dt.date) -> dict[str, float]:
    """The report's NET against the movement of the tax accounts (tax closing entries excluded)."""
    sudo = env.elevated("tax return")
    data = tax_report(env, company, date_from, date_to)
    totals = report_totals(data)
    accounts = tax_account_ids(sudo, company)
    ledger = 0.0
    if accounts:
        ledger = -float(
            sudo.cr.scalar(
                "SELECT COALESCE(SUM(balance), 0) FROM account_move_line WHERE company_id = :cid "
                "AND parent_state = 'posted' AND date >= :start AND date <= :end AND account_id = ANY(:accounts) "
                "AND NOT (move_id = ANY(:closing))",
                {
                    "cid": company.ids[0],
                    "start": date_from,
                    "end": date_to,
                    "accounts": accounts,
                    "closing": closing_move_ids(sudo, company),
                },
            )
            or 0
        )
    ccur = company.elevated().currency_id
    ledger = float(ccur.round(ledger))
    return {**totals, "ledger_net": ledger, "difference": float(ccur.round(totals["NET"] - ledger))}


def closing_lines(env: Any, company: Model, date_from: dt.date, date_to: dt.date) -> list[dict[str, Any]]:
    """Journal items of the closing entry (company currency): clear each tax account per tax group."""
    rows = env.cr.fetchall(
        """
        SELECT l.account_id, t.tax_group_id, SUM(l.balance)
          FROM account_move_line l
          JOIN account_tax_repartition_line r ON r.id = l.tax_repartition_line_id
          JOIN account_tax t ON t.id = l.tax_line_id
         WHERE l.company_id = :cid AND l.parent_state = 'posted' AND l.date >= :start AND l.date <= :end
           AND COALESCE(r.use_in_tax_closing, true)
         GROUP BY l.account_id, t.tax_group_id
         ORDER BY t.tax_group_id, l.account_id
        """,
        {"cid": company.ids[0], "start": date_from, "end": date_to},
    )
    ccur = company.elevated().currency_id
    groups = env["account.tax_group"].elevated()
    per_group: dict[int | None, float] = defaultdict(float)
    lines: list[dict[str, Any]] = []
    for account_id, group_id, balance in rows:
        amount = float(ccur.round(float(balance or 0)))
        if not amount:
            continue
        per_group[group_id] += amount
        lines.append({"account_id": int(account_id), "balance": -amount, "name": _("Tax closing")})
    for group_id, total in per_group.items():
        total = float(ccur.round(total))
        if not total:
            continue
        group = groups.browse([group_id]) if group_id else groups.browse([])
        account = group.tax_payable_account_id if total < 0 else group.tax_receivable_account_id
        if not account:
            raise UserError(
                _(
                    "Set the tax payable and receivable accounts of the tax group '{group}' to close the period",
                    group=group.name if group else "-",
                )
            )
        label = _("Tax payable: {group}", group=group.name) if total < 0 else _("Tax credit: {group}", group=group.name)
        lines.append({"account_id": account.ids[0], "balance": total, "name": label})
    return lines
