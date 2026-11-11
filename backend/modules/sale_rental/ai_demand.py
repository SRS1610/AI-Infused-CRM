"""Numerical core of the rental AI features (no ORM, no LLM).

**Demand forecast** (:func:`forecast`) — units out per day of one rentable product for the next weeks, from its daily
history (units with a reservation each day, :func:`modules.sale_rental.rental.rental_history`):

1. *Weekly profile*: for each weekday, the median over the recent weeks of (that day / its week's mean), normalised to
   average 1 (multiplicative seasonality; a flat profile while the history is too short or empty).
2. *Level and trend*: a Theil-Sen line (median of pairwise slopes, robust to one-off events) through the weekly
   means of the last :data:`TREND_WEEKS` weeks, evaluated at the last week; the trend is damped
   (:data:`DAMPING` per week) so a short burst does not run away.
3. *Forecast*: day = (level + damped trend) x weekday factor, clipped to [0, units]; an 80 % range from the
   in-sample residuals, widening with the horizon.

**Usage anomalies** (:func:`detect`) — transparent rules on the company's rentals and daily utilisation:

* ``late_returns``: a customer with at least :data:`LATE_MIN` late returns (or rentals overdue now) in the window and
  at least :data:`LATE_SHARE` of their rentals late;
* ``long_booking``: a booking whose duration is far above the product's usual durations (robust z-score of the
  log-duration ≥ :data:`LONG_Z` and more than :data:`LONG_RATIO` x the median, with :data:`LONG_MIN_REFERENCE`
  other bookings to compare with);
* ``utilisation_spike`` / ``utilisation_drop``: the last 7 days' units out are far from what the forecast made from
  the history before them expected (beyond 3 weekly standard deviations, :data:`SHIFT_SHARE` of the forecast and
  :data:`SHIFT_MIN` unit-days).
"""

from __future__ import annotations

import datetime as dt
import itertools
import math
import random
import statistics
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

MIN_HISTORY_DAYS = 14
PROFILE_WEEKS = 8
TREND_WEEKS = 12
DAMPING = 0.85
Z80 = 1.2816

LATE_MIN = 2
LATE_SHARE = 0.4
LATE_GRACE_HOURS = 1
LONG_Z = 3.0
LONG_RATIO = 2.5
LONG_MIN_REFERENCE = 6
SHIFT_SHARE = 0.35
SHIFT_MIN = 3.0
SHIFT_Z = 3.0


# ============================================================================================ forecast


def _median(values: Sequence[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def theil_sen(ys: Sequence[float]) -> tuple[float, float]:
    """(slope, value of the robust line at the last point) of equally spaced ``ys``."""
    n = len(ys)
    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return 0.0, float(ys[0])
    slopes = [(ys[j] - ys[i]) / (j - i) for i in range(n) for j in range(i + 1, n)]
    slope = _median(slopes)
    intercept = _median([y - slope * i for i, y in enumerate(ys)])
    return slope, intercept + slope * (n - 1)


def weekly_profile(history: Sequence[float], first_weekday: int) -> list[float]:
    """Seven multiplicative weekday factors (Monday first) averaging 1."""
    weeks = len(history) // 7
    if weeks < 2:
        return [1.0] * 7
    offset = len(history) - weeks * 7
    ratios: dict[int, list[float]] = defaultdict(list)
    for w in range(max(0, weeks - PROFILE_WEEKS), weeks):
        chunk = history[offset + 7 * w : offset + 7 * (w + 1)]
        mean = sum(chunk) / 7
        if mean <= 0:
            continue
        for k, v in enumerate(chunk):
            ratios[(first_weekday + offset + 7 * w + k) % 7].append(v / mean)
    if len(ratios) < 7:
        return [1.0] * 7
    raw = [_median(ratios[d]) for d in range(7)]
    avg = sum(raw) / 7
    return [r / avg for r in raw] if avg > 0 else [1.0] * 7


def forecast(
    history: Sequence[float],
    first_day: dt.date,
    *,
    weeks: int = 4,
    units: int | None = None,
) -> dict[str, Any]:
    """Daily and weekly units-out forecast for ``weeks`` weeks after the history (``history[i]`` = units out on
    ``first_day + i``). Weekly rows: ``unit_days`` (sum of the daily forecasts), ``average`` (units out per day),
    ``low`` / ``high`` (80 % range of the unit-days) and ``utilisation`` when ``units`` is known."""
    hist = [max(0.0, float(v)) for v in history]
    start = first_day + dt.timedelta(days=len(hist))
    cap = float(units) if units else None
    if len(hist) < MIN_HISTORY_DAYS:
        level = sum(hist) / len(hist) if hist else 0.0
        profile, slope, method = [1.0] * 7, 0.0, "average (short history)"
        sd = math.sqrt(level) if level else 0.0
    else:
        profile = weekly_profile(hist, first_day.weekday())
        n_weeks = len(hist) // 7
        offset = len(hist) - n_weeks * 7
        means = [sum(hist[offset + 7 * w : offset + 7 * (w + 1)]) / 7 for w in range(n_weeks)][-TREND_WEEKS:]
        slope, level = theil_sen(means)
        fitted = []
        for i, v in enumerate(hist[offset:]):
            week = i // 7
            base = means[week - (n_weeks - len(means))] if week >= n_weeks - len(means) else means[0]
            fitted.append(v - base * profile[(first_day.weekday() + offset + i) % 7])
        sd = statistics.pstdev(fitted) if len(fitted) > 1 else 0.0
        method = "weekly profile x damped robust trend"
    days = []
    for i in range(weeks * 7):
        h_week = i // 7 + 1
        trend = slope * sum(DAMPING**k for k in range(1, h_week + 1))
        value = max(0.0, (level + trend) * profile[(start + dt.timedelta(days=i)).weekday()])
        if cap is not None:
            value = min(cap, value)
        days.append({"date": (start + dt.timedelta(days=i)).isoformat(), "units_out": round(value, 2)})
    rows = []
    for w in range(weeks):
        chunk = days[7 * w : 7 * (w + 1)]
        total = sum(d["units_out"] for d in chunk)
        spread = Z80 * sd * math.sqrt(7) * math.sqrt(1.2 + 0.5 * w)
        row: dict[str, Any] = {
            "week": w + 1,
            "week_start": chunk[0]["date"],
            "week_end": chunk[-1]["date"],
            "unit_days": round(total, 1),
            "average": round(total / 7, 2),
            "low": round(max(0.0, total - spread), 1),
            "high": round(total + spread if cap is None else min(cap * 7, total + spread), 1),
        }
        if cap:
            row["utilisation"] = round(total / (cap * 7), 4)
        rows.append(row)
    return {
        "method": method,
        "history_days": len(hist),
        "first_forecast_day": start.isoformat(),
        "level": round(level, 2),
        "trend_per_week": round(slope, 3),
        "weekday_factors": [round(p, 3) for p in profile],
        "units": units,
        "weeks": rows,
        "days": days,
    }


def mape(predicted: Sequence[float], actual: Sequence[float]) -> float:
    pairs = [(p, a) for p, a in zip(predicted, actual, strict=True) if a]
    return sum(abs(p - a) / abs(a) for p, a in pairs) / len(pairs) if pairs else float("nan")


# ============================================================================================ anomalies


def _late(r: dict[str, Any], now: dt.datetime) -> bool:
    end = r.get("planned_end")
    if not end:
        return False
    grace = dt.timedelta(hours=LATE_GRACE_HOURS)
    if r.get("returned"):
        return r["returned"] > end + grace
    return r.get("status") in ("pickedup", "late") and now > end + grace


def _hours(r: dict[str, Any]) -> float:
    return (
        max(0.0, (r["planned_end"] - r["start"]).total_seconds() / 3600)
        if r.get("start") and r.get("planned_end")
        else 0.0
    )


def late_return_flags(rentals: list[dict[str, Any]], now: dt.datetime) -> list[dict[str, Any]]:
    by_customer: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rentals:
        if r.get("partner_id") and (r.get("returned") or r.get("status") in ("pickedup", "late")):
            by_customer[int(r["partner_id"])].append(r)
    flags = []
    for partner_id, rows in sorted(by_customer.items()):
        late = [r for r in rows if _late(r, now)]
        share = len(late) / len(rows)
        if len(late) >= LATE_MIN and share >= LATE_SHARE:
            worst = max(late, key=lambda r: ((r.get("returned") or now) - r["planned_end"]).total_seconds())
            flags.append(
                {
                    "kind": "late_returns",
                    "key": f"late_returns:partner:{partner_id}",
                    "partner_id": partner_id,
                    "partner": rows[0].get("partner", ""),
                    "line_id": worst.get("line_id"),
                    "order_id": worst.get("order_id"),
                    "product_id": worst.get("product_id"),
                    "score": round(share, 3),
                    "details": {
                        "late": len(late),
                        "rentals": len(rows),
                        "max_hours_late": round(
                            ((worst.get("returned") or now) - worst["planned_end"]).total_seconds() / 3600
                        ),
                    },
                }
            )
    return flags


def long_booking_flags(rentals: list[dict[str, Any]], since: dt.datetime | None = None) -> list[dict[str, Any]]:
    by_product: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for r in rentals:
        if r.get("product_id") and _hours(r) > 0:
            by_product[int(r["product_id"])].append(r)
    flags = []
    for product_id, rows in sorted(by_product.items()):
        for r in rows:
            if since and r["start"] < since:
                continue
            others = [math.log(_hours(o)) for o in rows if o is not r]
            if len(others) < LONG_MIN_REFERENCE:
                continue
            med = _median(others)
            mad = _median([abs(x - med) for x in others]) * 1.4826 or 0.25
            hours = _hours(r)
            z = (math.log(hours) - med) / mad
            if z >= LONG_Z and hours >= LONG_RATIO * math.exp(med):
                flags.append(
                    {
                        "kind": "long_booking",
                        "key": f"long_booking:line:{r.get('line_id')}",
                        "partner_id": r.get("partner_id"),
                        "partner": r.get("partner", ""),
                        "line_id": r.get("line_id"),
                        "order_id": r.get("order_id"),
                        "product_id": product_id,
                        "score": round(z, 2),
                        "details": {"days": round(hours / 24, 1), "usual_days": round(math.exp(med) / 24, 1)},
                    }
                )
    return flags


def utilisation_flags(products: dict[int, dict[str, Any]], day: dt.date) -> list[dict[str, Any]]:
    """``products``: ``{id: {"name", "units", "history": [units out per day, ending yesterday]}}``."""
    flags = []
    for product_id, p in sorted(products.items()):
        hist = [float(v) for v in p.get("history") or []]
        if len(hist) < 28 + 7:
            continue
        before, recent = hist[:-7], hist[-7:]
        first = day - dt.timedelta(days=len(hist))
        fc = forecast(before, first, weeks=1, units=p.get("units"))
        expected = fc["weeks"][0]["unit_days"]
        actual = sum(recent)
        n_weeks = len(before) // 7
        offset = len(before) - n_weeks * 7
        totals = [sum(before[offset + 7 * w : offset + 7 * (w + 1)]) for w in range(n_weeks)][-TREND_WEEKS:]
        diffs = [b - a for a, b in itertools.pairwise(totals)]
        sd = statistics.pstdev(diffs) / math.sqrt(2) if len(diffs) > 1 else 0.0
        gap = actual - expected
        if abs(gap) > max(SHIFT_Z * sd, SHIFT_SHARE * expected, SHIFT_MIN):
            kind = "utilisation_spike" if gap > 0 else "utilisation_drop"
            flags.append(
                {
                    "kind": kind,
                    "key": f"{kind}:product:{product_id}:{day.isoformat()}",
                    "partner_id": None,
                    "partner": "",
                    "line_id": None,
                    "order_id": None,
                    "product_id": product_id,
                    "score": round(abs(gap) / max(expected, 1.0), 3),
                    "details": {"actual": round(actual, 1), "expected": round(expected, 1), "units": p.get("units")},
                }
            )
    return flags


def detect(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Every anomaly of a company's usage facts: ``{"now", "day", "rentals": [...], "products": {...}}`` (see the
    extraction in ``ai_features.usage_facts``)."""
    now = facts["now"]
    day = facts["day"]
    rentals = facts.get("rentals") or []
    recent = now - dt.timedelta(days=int(facts.get("long_window_days", 30)))
    return (
        late_return_flags(rentals, now)
        + long_booking_flags(rentals, recent)
        + utilisation_flags(facts.get("products") or {}, day)
    )


# ============================================================================================ synthetic data

SHAPES = {
    "weekend": [0.7, 0.7, 0.8, 0.9, 1.3, 1.4, 1.2],  # event equipment
    "weekday": [1.2, 1.25, 1.2, 1.15, 1.05, 0.55, 0.6],  # site tools
    "flat": [1.0, 1.0, 1.0, 1.05, 1.05, 0.95, 0.95],
}


def synthetic_demand(rng: random.Random, weeks: int = 26, horizon: int = 4) -> dict[str, Any]:
    """A multi-month daily units-out history with weekly seasonality, trend and noise, and the next weeks' actuals."""
    units = rng.randint(6, 30)
    base = rng.uniform(0.3, 0.65) * units
    growth = rng.uniform(-0.006, 0.012)  # per week, relative to the base
    shape = SHAPES[rng.choice(list(SHAPES))]
    noise = rng.uniform(0.08, 0.16)
    days = 7 * (weeks + horizon)
    first = dt.date(2026, 1, 5) + dt.timedelta(days=rng.randint(0, 6))
    values = []
    for i in range(days):
        level = base * (1 + growth * i / 7) * shape[(first + dt.timedelta(days=i)).weekday()]
        value = rng.gauss(level, max(0.5, noise * level + 0.35 * math.sqrt(level)))
        if rng.random() < 0.01:  # a one-off event
            value *= 1.8
        values.append(int(min(units, max(0, round(value)))))
    cut = 7 * weeks
    return {
        "first_day": first.isoformat(),
        "units": units,
        "history": values[:cut],
        "actual": values[cut:],
        "actual_weeks": [sum(values[cut + 7 * w : cut + 7 * (w + 1)]) for w in range(horizon)],
    }
