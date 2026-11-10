"""Helpers for bank tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tessera.orm import Model

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"


def sample(name: str) -> bytes:
    return (SAMPLES / name).read_bytes()


def bank_journal(env: Any, company: Model | None = None) -> Model:
    company = company or env.company
    return env["account.journal"].search(
        [("type", "=", "bank"), ("company_id", "=", company.ids[0])], order="sequence, id", limit=1
    )


def statement(
    env: Any, lines: list[dict[str, Any]], *, journal: Model | None = None, post: bool = True, **vals: Any
) -> Model:
    journal = journal or bank_journal(env)
    total = sum(float(line["amount"]) for line in lines)
    st = env["account.bank_statement"].create(
        {
            "journal_id": journal.ids[0],
            "balance_start": 0.0,
            "balance_end_real": total,
            "line_ids": [("create", {"payment_ref": "/", **line}) for line in lines],
            **vals,
        }
    )
    if post:
        st.action_post()
    return st
