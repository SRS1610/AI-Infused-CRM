"""Acceptance 2-g: depreciation schedules match a golden table computed by hand; disposal posts gain/loss.

Every expected amount below was worked out by hand (the arithmetic is in the comments), not produced by the
code under test. Conventions (see ``modules/account_asset/board.py``):

* ``none``: full periods from the start of the acquisition period;
* ``constant_periods``: first period prorated on a 30/360 basis, one extra partial entry at the end;
* ``daily_computation``: linear amounts proportional to actual days;
* declining: ``residual x factor x fraction``, the last entry takes what is left;
* declining then straight line: the larger of the declining amount and residual / remaining periods.
"""

from __future__ import annotations

from typing import Any

import pytest

from modules.account.tests.common import invoice, partner
from modules.account_asset.tests.common import FAR, D, board, entry_lines, make_asset, month_ends

pytestmark = pytest.mark.db

YEARLY = {"method_period": "12"}
MONTHLY = {"method_period": "1"}

# 36 months from 16 January 2026, daily: 10 960.00 over 1 096 days (16/01/2026 - 15/01/2029) = 10.00 per day,
# so each month is 10 x its days in the life (January 2026: 16 days, January 2029: 15 days, February 2028: 29).
DAILY_36 = [
    160.0, 280.0, 310.0, 300.0, 310.0, 300.0, 310.0, 310.0, 300.0, 310.0, 300.0, 310.0,  # 2026
    310.0, 280.0, 310.0, 300.0, 310.0, 300.0, 310.0, 310.0, 300.0, 310.0, 300.0, 310.0,  # 2027
    310.0, 290.0, 310.0, 300.0, 310.0, 300.0, 310.0, 310.0, 300.0, 310.0, 300.0, 310.0,  # 2028
    150.0,  # 1-15 January 2029
]  # fmt: skip

GOLDEN: list[dict[str, Any]] = [
    {
        # 10 000 / 5 = 2 000 a year
        "id": "linear_5y_yearly",
        "vals": {
            "original_value": 10000.0,
            "acquisition_date": D(2024, 1, 1),
            "method": "linear",
            "method_number": 5,
            "prorata_computation_type": "none",
            **YEARLY,
        },
        "expected": [
            ("2024-12-31", 2000.0),
            ("2025-12-31", 2000.0),
            ("2026-12-31", 2000.0),
            ("2027-12-31", 2000.0),
            ("2028-12-31", 2000.0),
        ],
    },
    {
        # no prorata: an acquisition on 20 June still depreciates the full first year
        "id": "linear_no_prorata_mid_year",
        "vals": {
            "original_value": 9000.0,
            "acquisition_date": D(2026, 6, 20),
            "method": "linear",
            "method_number": 3,
            "prorata_computation_type": "none",
            **YEARLY,
        },
        "expected": [("2026-12-31", 3000.0), ("2027-12-31", 3000.0), ("2028-12-31", 3000.0)],
    },
    {
        "id": "linear_36m_monthly_daily_prorata_mid_month",
        "vals": {
            "original_value": 10960.0,
            "acquisition_date": D(2026, 1, 16),
            "method": "linear",
            "method_number": 36,
            "prorata_computation_type": "daily_computation",
            **MONTHLY,
        },
        "expected": list(zip([*month_ends(D(2026, 1, 1), 36), "2029-01-15"], DAILY_36, strict=True)),
    },
    {
        # 12 000 / 4 = 3 000 a year; 1 April - 31 December = 270/360 = 0.75 → 2 250; the last 0.25 year → 750
        "id": "constant_periods_yearly",
        "vals": {
            "original_value": 12000.0,
            "acquisition_date": D(2026, 4, 1),
            "method": "linear",
            "method_number": 4,
            "prorata_computation_type": "constant_periods",
            **YEARLY,
        },
        "expected": [
            ("2026-12-31", 2250.0),
            ("2027-12-31", 3000.0),
            ("2028-12-31", 3000.0),
            ("2029-12-31", 3000.0),
            ("2030-03-31", 750.0),
        ],
    },
    {
        # 1 000 a month; 16-31 January = 15/30 → 500; the last half month (1-15 January 2027) → 500
        "id": "constant_periods_monthly",
        "vals": {
            "original_value": 12000.0,
            "acquisition_date": D(2026, 1, 16),
            "method": "linear",
            "method_number": 12,
            "prorata_computation_type": "constant_periods",
            **MONTHLY,
        },
        "expected": [
            ("2026-01-31", 500.0),
            *[(d, 1000.0) for d in month_ends(D(2026, 2, 1), 11)],
            ("2027-01-15", 500.0),
        ],
    },
    {
        # residual x 30 %: 10 000 → 3 000, 7 000 → 2 100, 4 900 → 1 470, 3 430 → 1 029, last: 2 401 left
        "id": "degressive_30_5y",
        "vals": {
            "original_value": 10000.0,
            "acquisition_date": D(2026, 1, 1),
            "method": "degressive",
            "method_number": 5,
            "method_progress_factor": 0.3,
            "prorata_computation_type": "none",
            **YEARLY,
        },
        "expected": [
            ("2026-12-31", 3000.0),
            ("2027-12-31", 2100.0),
            ("2028-12-31", 1470.0),
            ("2029-12-31", 1029.0),
            ("2030-12-31", 2401.0),
        ],
    },
    {
        # half a first year: 10 000 x 30 % x 0.5 = 1 500; then 8 500 → 2 550, 5 950 → 1 785, 4 165 → 1 249.50,
        # 2 915.50 → 874.65; the last half year takes the 2 040.85 left
        "id": "degressive_30_prorata_half_year",
        "vals": {
            "original_value": 10000.0,
            "acquisition_date": D(2026, 7, 1),
            "method": "degressive",
            "method_number": 5,
            "method_progress_factor": 0.3,
            "prorata_computation_type": "constant_periods",
            **YEARLY,
        },
        "expected": [
            ("2026-12-31", 1500.0),
            ("2027-12-31", 2550.0),
            ("2028-12-31", 1785.0),
            ("2029-12-31", 1249.5),
            ("2030-12-31", 874.65),
            ("2031-06-30", 2040.85),
        ],
    },
    {
        # declining 40 % vs straight line on the residual:
        # 10 000: 4 000 vs 2 000 → 4 000; 6 000: 2 400 vs 1 500 → 2 400; 3 600: 1 440 vs 1 200 → 1 440;
        # 2 160: 864 vs 1 080 → 1 080 (straight line takes over); last: 1 080
        "id": "degressive_then_linear_40_5y",
        "vals": {
            "original_value": 10000.0,
            "acquisition_date": D(2026, 1, 1),
            "method": "degressive_then_linear",
            "method_number": 5,
            "method_progress_factor": 0.4,
            "prorata_computation_type": "none",
            **YEARLY,
        },
        "expected": [
            ("2026-12-31", 4000.0),
            ("2027-12-31", 2400.0),
            ("2028-12-31", 1440.0),
            ("2029-12-31", 1080.0),
            ("2030-12-31", 1080.0),
        ],
    },
    {
        # (12 000 - 2 000 salvage) / 5 = 2 000 a year; the book value ends at 2 000
        "id": "linear_salvage_value",
        "vals": {
            "original_value": 12000.0,
            "salvage_value": 2000.0,
            "acquisition_date": D(2024, 1, 1),
            "method": "linear",
            "method_number": 5,
            "prorata_computation_type": "none",
            **YEARLY,
        },
        "expected": [
            ("2024-12-31", 2000.0),
            ("2025-12-31", 2000.0),
            ("2026-12-31", 2000.0),
            ("2027-12-31", 2000.0),
            ("2028-12-31", 2000.0),
        ],
    },
    {
        # 1 000 / 3 = 333.33…: cumulative rounding gives 333.33, 333.34, 333.33 (total exactly 1 000)
        "id": "linear_rounding",
        "vals": {
            "original_value": 1000.0,
            "acquisition_date": D(2026, 1, 1),
            "method": "linear",
            "method_number": 3,
            "prorata_computation_type": "none",
            **YEARLY,
        },
        "expected": [("2026-12-31", 333.33), ("2027-12-31", 333.34), ("2028-12-31", 333.33)],
    },
]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["id"] for c in GOLDEN])
def test_golden_schedule(env, case):
    asset = make_asset(env, **case["vals"])
    expected = [(d, a) for d, a in case["expected"]]
    assert board(asset) == expected
    original = case["vals"]["original_value"]
    salvage = case["vals"].get("salvage_value", 0.0)
    assert round(sum(a for _d, a in expected), 2) == original - salvage
    # cumulative depreciation and remaining value on every entry
    running = 0.0
    for move in asset.depreciation_move_ids.sorted(lambda m: (m.date, m.ids[0])):
        running = round(running + float(move.asset_depreciation_value), 2)
        assert float(move.asset_depreciated_value) == running
        assert float(move.asset_remaining_value) == round(original - salvage - running, 2)
    # post the whole board: same amounts, book value = salvage value
    asset.action_post_due(FAR)
    assert board(asset, states=("posted",)) == expected
    assert float(asset.depreciated_value) == original - salvage
    assert float(asset.book_value) == salvage
    assert float(asset.value_residual) == 0.0
    first = asset.depreciation_move_ids.sorted(lambda m: m.date)[0]
    assert entry_lines(first) == {"6300": expected[0][1], "1510": -expected[0][1]}


def _post_until(asset, day: str) -> None:
    asset.action_post_due(day)


def test_mid_life_value_decrease(env):
    # 10 000 over 5 years; 2024 and 2025 posted (book 6 000); new value 3 000 on 31/12/2025:
    # impairment 3 000, then 3 000 over the 3 remaining years = 1 000 a year; book value ends at 0
    asset = make_asset(
        env,
        original_value=10000.0,
        acquisition_date=D(2024, 1, 1),
        method="linear",
        method_number=5,
        prorata_computation_type="none",
        method_period="12",
    )
    _post_until(asset, "2025-12-31")
    assert asset.action_modify(3000.0, "2025-12-31", "Damaged") == []
    assert board(asset) == [
        ("2024-12-31", 2000.0),
        ("2025-12-31", 2000.0),
        ("2025-12-31", 3000.0),
        ("2026-12-31", 1000.0),
        ("2027-12-31", 1000.0),
        ("2028-12-31", 1000.0),
    ]
    impairment = asset.depreciation_move_ids.filtered(lambda m: m.asset_value_change)
    assert impairment.state == "posted" and entry_lines(impairment) == {"6300": 3000.0, "1510": -3000.0}
    assert float(asset.book_value) == 3000.0
    asset.action_post_due(FAR)
    assert float(asset.book_value) == 0.0


def test_mid_period_value_decrease_prorates_first(env):
    # 12 000 over 12 months; Jan-Mar posted; on 15 April the value is set to 4 000:
    # 1-15 April = 15/30 of 1 000 = 500 → book 8 500; impairment 4 500; 4 000 over the 8.5 months left
    # (16-30 April = 0.5 → 235.29, then 470.59 / 470.58 alternately by cumulative rounding)
    asset = make_asset(
        env,
        original_value=12000.0,
        acquisition_date=D(2026, 1, 1),
        method="linear",
        method_number=12,
        prorata_computation_type="constant_periods",
        method_period="1",
    )
    _post_until(asset, "2026-03-31")
    asset.action_modify(4000.0, "2026-04-15")
    entries = board(asset)
    assert entries[:5] == [
        ("2026-01-31", 1000.0),
        ("2026-02-28", 1000.0),
        ("2026-03-31", 1000.0),
        ("2026-04-15", 500.0),
        ("2026-04-15", 4500.0),
    ]
    rest = entries[5:]
    assert [d for d, _a in rest] == ["2026-04-30", *month_ends(D(2026, 5, 1), 8)]
    assert rest[0][1] == 235.29  # 4 000 x 0.5 / 8.5
    assert round(sum(a for _d, a in rest), 2) == 4000.0


def test_pause_three_months(env):
    # 12 000 over 12 months from January 2026; Jan-Mar posted (3 000); paused on 15 April (1-15 April =
    # 15/30 → 500), resumed on 16 July: the 8.5 months left restart on 16 July (16-31 July = 0.5 → 500),
    # then 1 000 a month until March 2027 — the life ends three months later than planned
    asset = make_asset(
        env,
        original_value=12000.0,
        acquisition_date=D(2026, 1, 1),
        method="linear",
        method_number=12,
        prorata_computation_type="none",
        method_period="1",
    )
    _post_until(asset, "2026-03-31")
    asset.action_pause("2026-04-15")
    assert asset.state == "paused"
    assert board(asset) == [
        ("2026-01-31", 1000.0),
        ("2026-02-28", 1000.0),
        ("2026-03-31", 1000.0),
        ("2026-04-15", 500.0),
    ]
    assert float(asset.book_value) == 8500.0
    asset.action_resume("2026-07-16")
    assert asset.state == "open"
    planned = board(asset, states=("draft",))
    assert planned == [("2026-07-31", 500.0), *[(d, 1000.0) for d in month_ends(D(2026, 8, 1), 8)]]
    assert planned[-1][0] == "2027-03-31"
    asset.action_post_due(FAR)
    assert float(asset.book_value) == 0.0


def test_disposal_with_loss(env):
    # 12 000, salvage 2 000, 5 years: 2024 and 2025 posted (4 000); disposed of on 31 March 2026:
    # 2026 prorata 90/360 x 2 000 = 500 → accumulated 4 500, book value 7 500 = loss
    asset = make_asset(
        env,
        original_value=12000.0,
        salvage_value=2000.0,
        acquisition_date=D(2024, 1, 1),
        method="linear",
        method_number=5,
        prorata_computation_type="none",
        method_period="12",
    )
    _post_until(asset, "2025-12-31")
    move_id = asset.action_dispose("2026-03-31", "Scrapped")
    closing = env["account.move"].browse([move_id])
    assert closing.state == "posted"
    assert entry_lines(closing) == {"1500": -12000.0, "1510": 4500.0, "6930": 7500.0}
    assert board(asset) == [("2024-12-31", 2000.0), ("2025-12-31", 2000.0), ("2026-03-31", 500.0)]
    assert asset.state == "close" and asset.disposal_date == D(2026, 3, 31) and asset.disposal_move_id == closing
    assert not asset.depreciation_move_ids.filtered(lambda m: m.state == "draft")


def test_sale_with_gain(env):
    # 10 000 over 5 years: 2024, 2025 posted (4 000); sold on 30 June 2026 for 6 500:
    # 2026 prorata 180/360 x 2 000 = 1 000 → accumulated 5 000, book value 5 000, gain 1 500
    asset = make_asset(
        env,
        original_value=10000.0,
        acquisition_date=D(2024, 1, 1),
        method="linear",
        method_number=5,
        prorata_computation_type="none",
        method_period="12",
    )
    _post_until(asset, "2025-12-31")
    receipts = env.company.account_outstanding_receipts_id.code
    closing = env["account.move"].browse([asset.action_sell("2026-06-30", amount=6500.0)])
    assert entry_lines(closing) == {"1500": -10000.0, "1510": 5000.0, receipts: 6500.0, "4930": -1500.0}
    assert asset.state == "close"


def test_sale_from_invoice_line_with_loss(env):
    # same asset sold on 30 June 2026 on a customer invoice of 4 200 booked on 4900 (other income):
    # the proceeds leave 4900 and the 800 under the 5 000 book value is a loss
    asset = make_asset(
        env,
        original_value=10000.0,
        acquisition_date=D(2024, 1, 1),
        method="linear",
        method_number=5,
        prorata_computation_type="none",
        method_period="12",
    )
    _post_until(asset, "2025-12-31")
    other_income = env["account.account"].search([("code", "=", "4900"), ("company_id", "=", env.company.ids[0])])
    sale = invoice(
        env,
        partner(env, "Used equipment buyer"),
        [{"price_unit": 4200.0, "account_id": other_income.ids[0], "tax_ids": [("set", [])]}],
        invoice_date=D(2026, 6, 30),
    )
    line = sale.invoice_line_ids
    closing = env["account.move"].browse([asset.action_sell("2026-06-30", invoice_line_id=line.ids[0])])
    assert entry_lines(closing) == {"1500": -10000.0, "1510": 5000.0, "4900": 4200.0, "6930": 800.0}


def test_gross_increase_and_family_disposal(env):
    # 10 000 over 5 years, 2024 posted (book 8 000); revalued to 10 000 on 31/12/2024: a 2 000 gross increase
    # depreciated over the 4 remaining years (500 a year); the parent keeps 2 000 a year
    asset = make_asset(
        env,
        original_value=10000.0,
        acquisition_date=D(2024, 1, 1),
        method="linear",
        method_number=5,
        prorata_computation_type="none",
        method_period="12",
    )
    _post_until(asset, "2024-12-31")
    child_ids = asset.action_modify(10000.0, "2024-12-31", "Upgrade")
    child = env["account.asset"].browse(child_ids)
    assert child.parent_id == asset and float(child.original_value) == 2000.0 and child.state == "open"
    assert board(child) == [("2025-12-31", 500.0), ("2026-12-31", 500.0), ("2027-12-31", 500.0), ("2028-12-31", 500.0)]
    increase = child.original_move_line_ids.move_id
    assert entry_lines(increase) == {"1500": 2000.0, "4900": -2000.0}
    assert board(asset)[1:] == [
        ("2025-12-31", 2000.0),
        ("2026-12-31", 2000.0),
        ("2027-12-31", 2000.0),
        ("2028-12-31", 2000.0),
    ]
    # disposal on 31/12/2025 closes both: gross 12 000, accumulated 2 000 + 2 000 + 500, loss 7 500
    child.action_post_due("2025-12-31")
    asset.action_post_due("2025-12-31")
    closing = env["account.move"].browse([asset.action_dispose("2025-12-31")])
    assert entry_lines(closing) == {"1500": -12000.0, "1510": 4500.0, "6930": 7500.0}
    assert child.state == "close" and asset.state == "close"
