"""Scheduled bank job: apply the automatic reconciliation rules to every open transaction."""

from __future__ import annotations

from typing import Any

from tessera.jobs import job


@job("account_bank.auto_reconcile", queue="default", max_attempts=1, as_system=True)
def auto_reconcile_job(env: Any, journal_id: int | None = None) -> dict[str, Any]:
    from modules.account_bank.rules import auto_reconcile

    domain: list[Any] = [("state", "=", "posted")]
    if journal_id:
        domain.append(("journal_id", "=", int(journal_id)))
    return auto_reconcile(env["account.bank_statement_line"].elevated().search(domain, limit=5000))
