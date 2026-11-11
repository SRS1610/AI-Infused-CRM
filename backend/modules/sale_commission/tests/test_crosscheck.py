"""2-k: the demo plan's statements equal the independent spreadsheet, line by line and in total."""

from __future__ import annotations

import csv
import datetime as dt
import importlib.util
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.db

DATA = Path(__file__).with_name("data")
MONTH = dt.date(2026, 10, 1)


def _expected() -> list[dict[str, str]]:
    with (DATA / "commission_crosscheck.csv").open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_spreadsheet_is_reproducible() -> None:
    """The committed file is what the independent script computes."""
    spec = importlib.util.spec_from_file_location("make_crosscheck", DATA / "make_crosscheck.py")
    assert spec is not None and spec.loader is not None
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.render() == (DATA / "commission_crosscheck.csv").read_text(encoding="utf-8")


def test_demo_statements_equal_the_crosscheck(system_env: Any) -> None:
    from modules.sale_commission.demo import seed_commissions, seed_month

    statements = seed_commissions(system_env, MONTH)
    assert seed_month(system_env, MONTH) == 0  # idempotent
    assert len(statements) == 2
    by_login = {s.user_id.login: s for s in statements}
    assert set(by_login) == {"sam", "rita"}
    expected = _expected()
    for login, statement in by_login.items():
        assert (statement.date_from, statement.date_to) == (MONTH, dt.date(2026, 10, 31))
        rows = [r for r in expected if r["salesperson"] == login and r["document"] != "TOTAL"]
        total = next(r for r in expected if r["salesperson"] == login and r["document"] == "TOTAL")
        got = []
        for line in statement.line_ids.sorted(lambda r: (r.date, r.move_id.ids[0], r.move_line_id.ids[0])):
            got.append(
                [
                    login,
                    line.move_id.ref.rsplit("-", 1)[-1],
                    line.product_id.default_code,
                    f"{line.quantity:g}",
                    f"{line.base_amount:.2f}",
                    f"{line.rate:g}",
                    f"{line.fixed_amount:.2f}",
                    f"{line.raw_amount:.2f}",
                    f"{line.multiplier:g}",
                    f"{line.amount:.2f}",
                ]
            )
        want = [
            [
                r["salesperson"],
                r["document"],
                r["product"],
                r["quantity"],
                r["base"],
                r["rate"],
                r["fixed_per_unit"],
                r["raw"],
                r["multiplier"],
                r["commission"],
            ]
            for r in rows
        ]
        assert sorted(got) == sorted(want)
        assert f"{statement.base_amount:.2f}" == total["base"]
        assert f"{statement.achievement:.2f}" == total["rate"]  # the TOTAL row's rate column is the achievement
        assert f"{statement.multiplier:g}" == total["multiplier"]
        assert f"{statement.commission_amount:.2f}" == total["commission"]
        assert f"{statement.total_amount:.2f}" == total["commission"]
        # the explanation's facts carry the same numbers
        facts = statement.facts()
        assert f"{facts['commission']:.2f}" == total["commission"] and f"{facts['base_total']:.2f}" == total["base"]
        assert facts["multiplier"] == statement.multiplier and facts["lines"] == len(rows)
    # recomputing changes nothing
    before = [(s.ids[0], s.commission_amount, len(s.line_ids)) for s in statements]
    statements.action_compute()
    assert [(s.ids[0], s.commission_amount, len(s.line_ids)) for s in statements] == before
