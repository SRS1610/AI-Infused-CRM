"""Verification of the hash chain of posted entries (D-073)."""

from __future__ import annotations

import hashlib
from typing import Any

from tessera.orm import Model


def check_integrity(env: Any, journal: Model) -> dict[str, Any]:
    """Recompute the chain of ``journal``; report the first entry whose hash no longer matches."""
    moves = (
        env["account.move"]
        .elevated("integrity check")
        .with_context(active_test=False)
        .search([("journal_id", "=", journal.ids[0]), ("inalterable_hash", "!=", None)], order="secure_sequence_number")
    )
    previous = ""
    checked = 0
    for move in moves:
        expected = hashlib.sha256((previous + move._canonical()).encode()).hexdigest()
        if expected != move.inalterable_hash:
            return {"ok": False, "checked": checked, "first_broken": move.name, "journal": journal.code}
        previous = move.inalterable_hash
        checked += 1
    return {"ok": True, "checked": checked, "first_broken": None, "journal": journal.code}


def check_company(env: Any, company: Model) -> list[dict[str, Any]]:
    journals = (
        env["account.journal"]
        .elevated()
        .search([("company_id", "=", company.ids[0]), ("restrict_mode_hash_table", "=", True)])
    )
    return [check_integrity(env, journal) for journal in journals]
