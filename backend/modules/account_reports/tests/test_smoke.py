"""Report definitions load and every report runs on an empty or demo ledger."""

from __future__ import annotations

import pytest

from modules.account_reports.engine import list_reports, run_report

pytestmark = pytest.mark.db

CODES = (
    "balance_sheet",
    "profit_loss",
    "cash_flow",
    "trial_balance",
    "general_ledger",
    "partner_ledger",
    "aged_receivable",
    "aged_payable",
    "tax",
)


def test_definitions_are_consistent(env):
    reports = env["account.report"].search([])
    assert {"balance_sheet", "profit_loss", "cash_flow", "tax_eu", "tax_us", "tax_generic"} <= set(
        reports.mapped("code")
    )
    assert reports.check_definition()


@pytest.mark.parametrize("code", CODES)
def test_every_report_runs(env, code):
    data = run_report(env, {"report": code})
    assert data["columns"]
    assert isinstance(data["lines"], list)
    assert data["report"]["requested"] == code


def test_list_reports(env):
    codes = [r["code"] for r in list_reports(env)]
    assert codes[:3] == ["balance_sheet", "profit_loss", "cash_flow"]
    assert "tax" in codes
