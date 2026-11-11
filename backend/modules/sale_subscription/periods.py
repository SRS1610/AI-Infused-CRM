"""Billing periods and proration arithmetic (pure functions, no database).

Billing grid. A subscription's periods start on ``anchor + k x period`` (k = 0, 1, 2 …), computed from the
anchor each time rather than chained, so a monthly subscription anchored on the 31st bills on the 31st, or on
the last day of shorter months, and comes back to the 31st (Jan 31 → Feb 28 → Mar 31 → Apr 30 …). A period
runs from its start to the day before the next start, both included.

Day-count rule. Prorations count calendar days, both ends included: a change effective on day ``C`` applies
to the days ``C … period end``, so ``remaining = period_end - C + 1`` and ``ratio = remaining / period_days``
where ``period_days`` is the length of the whole period (28 to 31 days for a month, 365 or 366 for a year, 7
for a week). The prorated amount of a line is rounded to the currency's precision.

Monthly normalisation (MRR). A period amount is converted to a monthly amount with: monthly x 1/value,
yearly x 1/(12 x value), weekly x 52/(12 x value).
"""

from __future__ import annotations

import datetime as dt

from dateutil.relativedelta import relativedelta

UNITS = (("week", "Weeks"), ("month", "Months"), ("year", "Years"))
ONE_DAY = dt.timedelta(days=1)


def shift(day: dt.date, unit: str, count: int) -> dt.date:
    """``day`` moved by ``count`` units (months and years clamp to the end of shorter months)."""
    if unit == "week":
        return day + dt.timedelta(weeks=count)
    if unit == "month":
        return day + relativedelta(months=count)
    if unit == "year":
        return day + relativedelta(years=count)
    raise ValueError(f"unknown billing period unit {unit!r}")


def grid_date(anchor: dt.date, unit: str, value: int, index: int) -> dt.date:
    """Start of the ``index``-th period after ``anchor``."""
    return shift(anchor, unit, value * index)


def grid_index(anchor: dt.date, unit: str, value: int, day: dt.date) -> int:
    """Index of the first period starting on or after ``day``."""
    if day <= anchor:
        return 0
    # a cheap estimate, then walk: periods are at least 7 days long
    days = (day - anchor).days
    approx = {"week": 7, "month": 28, "year": 365}[unit] * max(int(value), 1)
    index = max(days // (approx + 3) - 1, 0)
    while grid_date(anchor, unit, value, index) < day:
        index += 1
    while index > 0 and grid_date(anchor, unit, value, index - 1) >= day:
        index -= 1
    return index


def next_start_on_or_after(anchor: dt.date, unit: str, value: int, day: dt.date) -> dt.date:
    """The first period start of the grid on or after ``day``."""
    return grid_date(anchor, unit, value, grid_index(anchor, unit, value, day))


def period_of(anchor: dt.date, unit: str, value: int, start: dt.date) -> tuple[dt.date, dt.date, int]:
    """(start, end, length in days) of the period starting on ``start``.

    ``start`` is normally on the grid; if it is not (the grid changed), the period is one unit long from it.
    """
    index = grid_index(anchor, unit, value, start)
    if grid_date(anchor, unit, value, index) == start:
        following = grid_date(anchor, unit, value, index + 1)
    else:
        following = shift(start, unit, value)
    end = following - ONE_DAY
    return start, end, (following - start).days


def monthly_factor(unit: str, value: int) -> float:
    """Multiply a per-period amount by this to get a monthly amount."""
    value = max(int(value or 1), 1)
    if unit == "week":
        return 52.0 / (12.0 * value)
    if unit == "year":
        return 1.0 / (12.0 * value)
    return 1.0 / value


def remaining_ratio(effective: dt.date, period_end: dt.date, period_days: int) -> tuple[int, float]:
    """(days, ratio) of a period from ``effective`` to ``period_end``, both included."""
    days = max((period_end - effective).days + 1, 0)
    return days, (days / period_days if period_days else 0.0)


def renewal_term(start: dt.date, end: dt.date) -> tuple[dt.date, dt.date]:
    """(start, end) of the term that follows ``start … end``, with the same length.

    Whole months or years are kept as such (Jan 1 - Dec 31 renews as Jan 1 - Dec 31); other terms keep their
    number of days.
    """
    new_start = end + ONE_DAY
    delta = relativedelta(new_start, start)
    if not delta.days and (delta.months or delta.years):
        return new_start, new_start + delta - ONE_DAY
    return new_start, new_start + (end - start)
