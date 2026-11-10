"""Importing statement files into a bank journal.

The file is parsed (``importers``), checked against the journal (account number, currency, balances adding
up) and turned into statements; transactions already imported in the journal (same ``unique_import_id``)
are skipped, so importing the same file twice adds nothing. Statements are posted and the automatic rules
run unless asked otherwise.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from modules.account_bank.importers import parse_file
from modules.account_bank.importers.base import ParsedStatement, sanitize_account
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model


def _check_journal(journal: Model, st: ParsedStatement) -> None:
    journal = journal.elevated()
    expected = journal.bank_account_id.sanitized_acc_number if journal.bank_account_id else None
    if expected and st.account_number and sanitize_account(st.account_number) != expected:
        raise UserError(
            _(
                "The statement is for account {found}, but journal {journal} is linked to {expected}",
                found=st.account_number,
                journal=journal.name,
                expected=journal.bank_account_id.acc_number,
            )
        )
    currency = journal._bank_currency()
    if st.currency and st.currency.upper() != currency.name:
        raise UserError(
            _(
                "The statement is in {found}, but journal {journal} is kept in {expected}",
                found=st.currency,
                journal=journal.name,
                expected=currency.name,
            )
        )


def _currency_id(env: Any, code: str | None) -> int | None:
    if not code:
        return None
    currency = (
        env["core.currency"].elevated().with_context(active_test=False).search([("name", "=", code.upper())], limit=1)
    )
    return currency.ids[0] if currency else None


def import_statements(
    journal: Model,
    content: bytes,
    filename: str | None = None,
    *,
    file_format: str | None = None,
    options: dict[str, Any] | None = None,
    attachment: Model | None = None,
    post: bool = True,
    auto_reconcile: bool = True,
) -> dict[str, Any]:
    """Import a statement file into ``journal``; returns what was created and skipped."""
    journal.ensure_one()
    env = journal.env
    if journal.type not in ("bank", "cash"):
        raise UserError(_("Statements are imported into a bank or cash journal"))
    if options is None and journal.elevated().bank_csv_options:
        options = dict(journal.elevated().bank_csv_options)
    kind, parsed = parse_file(content, filename, file_format=file_format, options=options)
    prefix = sanitize_account(next((s.account_number for s in parsed if s.account_number), None)) or ""
    statements = env["account.bank_statement"]
    lines_model = env["account.bank_statement_line"]
    created_lines = 0
    skipped = 0
    for st in parsed:
        _check_journal(journal, st)
        st.fill_unique_ids(prefix)
        ids = [t.unique_import_id for t in st.transactions]
        existing = set(
            lines_model.elevated()
            .search([("journal_id", "=", journal.ids[0]), ("unique_import_id", "in", ids)])
            .mapped("unique_import_id")
        )
        fresh = [t for t in st.transactions if t.unique_import_id not in existing]
        skipped += len(st.transactions) - len(fresh)
        if not fresh:
            continue
        balance_start = st.balance_start or Decimal(0)
        balance_end = st.balance_end or Decimal(0)
        if not st.balances_in_file:
            balance_start = Decimal(str(journal._last_balance()))
            balance_end = balance_start + sum((t.amount for t in fresh), Decimal(0))
        elif len(fresh) != len(st.transactions):
            # part of the file was imported before: the new statement holds the remaining transactions
            balance_start = balance_end - sum((t.amount for t in fresh), Decimal(0))
        statement = statements.create(
            {
                "name": st.name or "/",
                "journal_id": journal.ids[0],
                "date": st.date or max(t.date for t in fresh),
                "balance_start": float(balance_start),
                "balance_end_real": float(balance_end),
                "import_file_id": attachment.ids[0] if attachment else None,
                "line_ids": [
                    (
                        "create",
                        {
                            "sequence": i,
                            "date": t.date,
                            "amount": float(t.amount),
                            "payment_ref": t.payment_ref or t.ref or "/",
                            "ref": t.ref,
                            "partner_name": t.partner_name,
                            "account_number": t.account_number,
                            "transaction_type": t.transaction_type,
                            "unique_import_id": t.unique_import_id,
                            "foreign_currency_id": _currency_id(env, t.foreign_currency),
                            "amount_currency": float(t.amount_currency) if t.amount_currency is not None else 0.0,
                            "narration": t.narration,
                            "journal_id": journal.ids[0],
                        },
                    )
                    for i, t in enumerate(fresh, start=1)
                ],
            }
        )
        created_lines += len(fresh)
        statements |= statement
    result: dict[str, Any] = {
        "format": kind,
        "statement_ids": statements.ids,
        "lines": created_lines,
        "duplicates": skipped,
        "reconciled": 0,
    }
    if post and statements:
        statements.action_post()
        if auto_reconcile:
            from modules.account_bank.rules import auto_reconcile as run_rules

            result["reconciled"] = run_rules(statements.mapped("line_ids"))["reconciled"]
    return result
