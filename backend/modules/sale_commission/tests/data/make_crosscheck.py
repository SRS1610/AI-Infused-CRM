"""Independent cross-check of the demo commission plan (acceptance criterion 2-k).

Recomputes the demo month's statements from the plain document list below — quantities, unit prices and
discounts as agreed with the customer — with nothing but ``decimal``: it does not import Tessera, read the
database or call the module. ``python make_crosscheck.py`` rewrites ``commission_crosscheck.csv`` next to it;
the test suite checks that the module's statements equal that file line by line and in total, and that this
script still produces the committed file.

The plan (``data/demo/plan.json``): monthly, invoiced basis, target 50,000 per salesperson; robot arm 2 % + 100 per
unit, services category 5 %, everything else 3 %; accelerators x 0.8 from 0 %, x 1 from 80 %, x 1.2 from 100 %.
"""

from __future__ import annotations

import csv
import sys
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

D = Decimal
CENT = D("0.01")
TARGET = D(50000)
BANDS = [(D(0), D("0.8")), (D(80), D(1)), (D(100), D("1.2"))]
CATEGORY = {
    "HW-ARM6": "goods",
    "HW-GRIP": "goods",
    "HW-FTS": "goods",
    "SRV-CONS": "services",
    "SRV-ONB": "services",
    "SRV-SUP": "services",
}


def rate(code: str) -> tuple[Decimal, Decimal]:
    """(rate %, fixed per unit) of a product code."""
    if code == "HW-ARM6":
        return D(2), D(100)
    if CATEGORY[code] == "services":
        return D(5), D(0)
    return D(3), D(0)


# (salesperson, document, sign, product code, quantity, unit price, discount %) — the documents that count
LINES = [
    ("hugo", "A1", 1, "HW-ARM6", D(2), D(18900), D(5)),
    ("hugo", "A1", 1, "SRV-ONB", D(1), D(2500), D(0)),
    ("hugo", "A2", 1, "SRV-CONS", D(10), D(1200), D(0)),
    ("hugo", "A2", 1, "HW-GRIP", D(3), D(1450), D("12.5")),
    ("hugo", "A3", -1, "SRV-CONS", D(2), D(1200), D(0)),
    ("nadia", "B1", 1, "SRV-SUP", D(1), D(4800), D(0)),
    ("nadia", "B1", 1, "HW-FTS", D(3), D(3200), D("7.5")),
    ("nadia", "B2", 1, "HW-ARM6", D(1), D(18900), D(0)),
    ("nadia", "B3", -1, "HW-FTS", D(1), D(3200), D(0)),
]
HEADER = [
    "salesperson",
    "document",
    "product",
    "quantity",
    "base",
    "rate",
    "fixed_per_unit",
    "raw",
    "multiplier",
    "commission",
]


def half_up(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def rows() -> list[list[str]]:
    out: list[list[str]] = []
    for person in ("nadia", "hugo"):
        mine = [line for line in LINES if line[0] == person]
        bases = [sign * half_up(qty * price * (1 - disc / 100)) for _p, _d, sign, _c, qty, price, disc in mine]
        achievement = sum(bases, D(0)) / TARGET * 100
        multiplier = max(m for lo, m in BANDS if lo <= achievement)
        total = D(0)
        for (_p, doc, sign, code, qty, _price, _disc), base in zip(mine, bases, strict=True):
            pct, fixed = rate(code)
            raw = base * pct / 100 + sign * qty * fixed
            commission = half_up(raw * multiplier)
            total += commission
            out.append(
                [
                    person,
                    doc,
                    code,
                    f"{qty}",
                    f"{base:.2f}",
                    f"{pct}",
                    f"{fixed:.2f}",
                    f"{half_up(raw):.2f}",
                    f"{multiplier}",
                    f"{commission:.2f}",
                ]
            )
        out.append(
            [
                person,
                "TOTAL",
                "",
                "",
                f"{sum(bases, D(0)):.2f}",
                f"{half_up(achievement):.2f}",
                "",
                "",
                f"{multiplier}",
                f"{total:.2f}",
            ]
        )
    return out


def render() -> str:
    from io import StringIO

    buf = StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(HEADER)
    writer.writerows(rows())
    return buf.getvalue()


if __name__ == "__main__":
    target = Path(__file__).with_name("commission_crosscheck.csv")
    target.write_text(render(), encoding="utf-8")
    sys.stdout.write(render())
