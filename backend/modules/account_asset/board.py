"""Depreciation schedules (pure functions, no database).

Vocabulary
----------
* **Period**: one month (``method_period = 1``) or one fiscal year (``method_period = 12``). An entry is dated
  at the end of the span it depreciates: the period end, or an earlier day for a last, partial span.
* **Life units**: what the useful life is measured in. With ``prorata_computation_type`` ``none`` or
  ``constant_periods`` a unit is one period (the life is ``method_number`` units); with
  ``daily_computation`` a unit is one day (the life is the number of days from the prorata date to the
  same day ``method_number`` periods later). A pause does not consume units, so the end of life moves.
* **Fraction of a span**: share of a full period it covers. ``constant_periods`` (and ``none`` when a
  schedule restarts mid-period) count days on a 30/360 basis — every month has 30 days, so half of any month
  is 0.5; ``daily_computation`` uses actual days.

Computation types
-----------------
* ``none`` — no prorata: depreciation starts on the first day of the period that contains the prorata date;
  ``method_number`` full periods.
* ``constant_periods`` — the first period is prorated on a 30/360 basis from the prorata date; one more,
  partial entry at the end completes the life (``method_number`` units in total).
* ``daily_computation`` — linear amounts are proportional to actual days; the life ends the day before the
  prorata date's anniversary after ``method_number`` periods.

Methods (``R`` = residual depreciable value = book value - salvage value at the start of a span)
-------------------------------------------------------------------------------------------------
* ``linear`` — ``R x weight / Σ remaining weights`` (weight = units), rounded on the cumulative amount so the
  rounded entries always add up to the depreciable value exactly.
* ``degressive`` — ``R x rate x fraction`` with ``rate = method_progress_factor x months / 12``; the last span
  depreciates whatever is left, so the book value ends at the salvage value.
* ``degressive_then_linear`` — the larger of the degressive amount and the linear amount over the remaining
  life (``R x weight / Σ remaining weights``): linear takes over once it gives more.
"""

from __future__ import annotations

import calendar
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass

EPS = 1e-6
Bounds = Callable[[dt.date], tuple[dt.date, dt.date]]


@dataclass(frozen=True)
class Span:
    start: dt.date
    end: dt.date
    units: float  # life units consumed
    fraction: float  # share of a full period (degressive rate multiplier)


@dataclass(frozen=True)
class Entry:
    date: dt.date
    start: dt.date
    units: float
    amount: float


def month_end(day: dt.date) -> dt.date:
    return day.replace(day=calendar.monthrange(day.year, day.month)[1])


def add_months(day: dt.date, months: int) -> dt.date:
    month0 = day.month - 1 + months
    year, month = day.year + month0 // 12, month0 % 12 + 1
    return dt.date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def monthly_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    return day.replace(day=1), month_end(day)


def calendar_year_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
    return dt.date(day.year, 1, 1), dt.date(day.year, 12, 31)


def days360(start: dt.date, end: dt.date) -> int:
    """Days from ``start`` to ``end`` inclusive, counting every month as 30 days (a month end counts as 30)."""
    d1 = min(start.day, 30)
    d2 = 30 if end == month_end(end) else min(end.day, 30)
    return (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1) + 1


def end_after_days360(start: dt.date, days: float) -> dt.date:
    """Last day of a span of ``days`` 30/360 days starting on ``start``."""
    total = (min(start.day, 30) - 1) + round(days)
    months, index = divmod(total, 30)
    first = add_months(start.replace(day=1), months)
    if index == 0:
        return first - dt.timedelta(days=1)
    return first.replace(day=min(index, calendar.monthrange(first.year, first.month)[1]))


def life_units(computation: str, prorata: dt.date, number: int, months: int) -> float:
    if computation == "daily_computation":
        return float((add_months(prorata, number * months) - prorata).days)
    return float(number)


def span_units(computation: str, start: dt.date, end: dt.date, bounds: Bounds, months: int) -> tuple[float, float]:
    """(units, fraction) of the span [start, end] lying within one period."""
    period_start, period_end = bounds(start)
    if computation == "daily_computation":
        days = (end - start).days + 1
        return float(days), days / ((period_end - period_start).days + 1)
    if start == period_start and end == period_end:
        return 1.0, 1.0
    fraction = days360(start, end) / (30.0 * months)
    return fraction, fraction


def spans(computation: str, start: dt.date, remaining: float, bounds: Bounds, months: int) -> list[Span]:
    """Split the remaining life (``remaining`` units from ``start``) into per-period spans."""
    out: list[Span] = []
    cur = start
    left = remaining
    while left > EPS:
        _ps, pe = bounds(cur)
        units, fraction = span_units(computation, cur, pe, bounds, months)
        end = pe
        if units >= left - EPS:
            if computation == "daily_computation":
                end = cur + dt.timedelta(days=round(left) - 1)
            elif units > left + EPS:
                end = min(pe, end_after_days360(cur, left * 30 * months))
            fraction = fraction * left / units if units else 0.0
            units = left
        out.append(Span(cur, end, units, fraction))
        left -= units
        cur = end + dt.timedelta(days=1)
        if len(out) > 5000:  # pragma: no cover - guards against a malformed life
            break
    return out


def schedule(
    *,
    method: str,
    residual: float,
    remaining_units: float,
    start: dt.date,
    computation: str,
    months: int,
    factor: float,
    bounds: Bounds,
    round_amount: Callable[[float], float],
) -> list[Entry]:
    """The entries depreciating ``residual`` over the remaining life starting on ``start``."""
    if residual <= EPS or remaining_units <= EPS:
        return []
    parts = spans(computation, start, remaining_units, bounds, months)
    rate = factor * months / 12.0
    total_weight = sum(p.units for p in parts)
    entries: list[Entry] = []
    left = round_amount(residual)
    done_weight = 0.0
    previous_cumulative = 0.0
    weight_left = total_weight
    for index, part in enumerate(parts):
        last = index == len(parts) - 1
        if last:
            amount = left
        elif method == "linear":
            done_weight += part.units
            cumulative = round_amount(residual * done_weight / total_weight)
            amount = cumulative - previous_cumulative
            previous_cumulative = cumulative
        else:
            degressive = left * rate * part.fraction
            if method == "degressive_then_linear":
                linear = left * part.units / weight_left if weight_left > EPS else left
                degressive = max(degressive, linear)
            amount = round_amount(degressive)
        amount = round_amount(min(amount, left))
        weight_left -= part.units
        left = round_amount(left - amount)
        entries.append(Entry(part.end, part.start, part.units, amount))
    return entries
