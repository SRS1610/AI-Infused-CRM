"""Unrealised exchange differences (2-c): month-end revaluation of open foreign-currency items.

For each company and foreign currency, the open receivable / payable items (and items on bank and cash
accounts in a foreign currency) are valued at the closing rate of the month. The difference with their book
value is posted on the last day of the month on the same accounts (exchange gain / loss as counterpart) and
reversed on the first day of the next month; each revaluation line is matched with its reversal so open items
and aged balances are unchanged. Running the job again for the same month replaces the previous pair.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections import defaultdict
from typing import Any

from modules.account.models.move import INTERNAL
from tessera.i18n import _
from tessera.jobs import job
from tessera.orm import Model


def _month_end(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def _ref(month_end: dt.date) -> str:
    return f"FX revaluation {month_end:%Y-%m}"


def revalue(company: Model, month_end: dt.date) -> Model | None:
    """Post the revaluation of ``company`` at ``month_end`` and its reversal. Returns the revaluation entry."""
    env = company.env
    company = company.elevated("fx revaluation")
    ccur = company.currency_id
    move_model = env["account.move"].elevated("fx revaluation")
    for old in move_model.search(
        [("company_id", "=", company.ids[0]), ("ref", "=", _ref(month_end)), ("state", "=", "posted")]
    ):
        pair = old | old.reversal_move_ids.filtered(lambda m: m.state == "posted")
        pair.mapped("line_ids").filtered(
            lambda line: line.matched_debit_ids or line.matched_credit_ids
        ).remove_move_reconcile()
        for move in pair:
            move.button_cancel()
    lines = (
        env["account.move_line"]
        .elevated()
        .search(
            [
                ("company_id", "=", company.ids[0]),
                ("parent_state", "=", "posted"),
                ("date", "<=", month_end),
                ("currency_id", "!=", ccur.ids[0]),
                ("reconciled", "=", False),
                ("account_id.account_type", "in", ["asset_receivable", "liability_payable", "asset_cash"]),
            ]
        )
    )
    groups: dict[tuple[int, int, int | None], dict[str, float]] = defaultdict(lambda: {"cur": 0.0, "book": 0.0})
    for line in lines:
        if line.move_id.is_fx_revaluation:
            continue
        account = line.account_id
        if account.account_type == "asset_cash":
            # bank and cash accounts: the whole balance, only when the account itself is kept in that currency
            if not account.currency_id or account.currency_id.ids != line.currency_id.ids:
                continue
            residual_cur, residual = float(line.amount_currency), float(line.balance)
        else:
            residual_cur, residual = float(line.amount_residual_currency), float(line.amount_residual)
        key = (account.ids[0], line.currency_id.ids[0], line.partner_id.ids[0] if line.partner_id else None)
        groups[key]["cur"] += residual_cur
        groups[key]["book"] += residual
    move_lines: list[tuple[str, dict[str, Any]]] = []
    gain = loss = 0.0
    currencies = env["core.currency"].elevated()
    for (account_id, currency_id, partner_id), values in groups.items():
        currency = currencies.browse([currency_id])
        value = float(currency.convert(values["cur"], ccur, company, month_end))
        diff = float(ccur.round(value - values["book"]))
        if not diff:
            continue
        move_lines.append(
            (
                "create",
                {
                    "name": _("Unrealised exchange difference ({currency})", currency=currency.name),
                    "account_id": account_id,
                    "partner_id": partner_id,
                    "balance": diff,
                    "amount_currency": 0.0,
                    "currency_id": currency_id,
                },
            )
        )
        if diff > 0:
            gain += diff
        else:
            loss += -diff
    if not move_lines:
        return None
    for amount, account, sign in (
        (gain, company.income_currency_exchange_account_id, -1),
        (loss, company.expense_currency_exchange_account_id, 1),
    ):
        if amount:
            move_lines.append(
                (
                    "create",
                    {
                        "name": _("Unrealised exchange difference"),
                        "account_id": account.ids[0],
                        "balance": sign * float(ccur.round(amount)),
                        "amount_currency": sign * float(ccur.round(amount)),
                        "currency_id": ccur.ids[0],
                    },
                )
            )
    from modules.account.reconcile import _exchange_journal

    journal = _exchange_journal(company)
    move = move_model.create(
        {
            "move_type": "entry",
            "journal_id": journal.ids[0],
            "company_id": company.ids[0],
            "currency_id": ccur.ids[0],
            "date": month_end,
            "ref": _ref(month_end),
            "line_ids": move_lines,
        }
    )
    move.with_context(**{INTERNAL: True}).write({"is_fx_revaluation": True})
    move.action_post()
    reversal = move_model.browse(
        move.action_reverse(date=(month_end + dt.timedelta(days=1)).isoformat(), mode="refund")
    )
    reversal.with_context(**{INTERNAL: True}).write({"is_fx_revaluation": True})
    reversal.action_post()
    reversal._reconcile_with(move)
    return move


@job("account.fx_revaluation", queue="default", max_attempts=1, as_system=True)
def fx_revaluation_job(env: Any, month_end: str | None = None) -> dict[str, Any]:
    """Revalue every company at the end of the previous month (or ``month_end``)."""
    if month_end:
        day = dt.date.fromisoformat(month_end)
    else:
        first = env.today().replace(day=1)
        day = first - dt.timedelta(days=1)
    day = _month_end(day)
    done: dict[str, Any] = {}
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        move = revalue(company, day)
        done[company.name] = move.name if move else None
    return {"month_end": day.isoformat(), "entries": done}
