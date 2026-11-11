"""Rental durations, best price for a duration and late fees (pure functions, no database).

Durations are measured on the wall clock of a time zone (the order's salesperson's, see
``QuotationLine._rental_tz``): from 10:00 on Saturday to 10:00 on Sunday is one day even when the clocks change
during the night, so a customer is never charged an extra hour (or given one) by a daylight-saving switch. A
started hour counts as a full hour.

**Best price.** Each pricing rule rents a block of ``duration x unit`` hours (hour = 1, day = 24, week = 168,
month = 720, i.e. a month is 30 days) for its price. The price of a rental of *H* hours is the cheapest
combination of blocks, any rule any number of times, that covers at least *H* hours:

    cost(0) = 0
    cost(h) = min over rules r of cost(max(0, h - hours(r))) + price(r)

computed by dynamic programming over the hours (``best_price``). So 9 days cost 1 week + 2 days when that is
cheaper than 2 weeks, 2 weeks when they are cheaper than 1 week + 2 days, and 3 hours cost 1 day when there is no
hourly rule. Ties go to the combination with fewer blocks.

**Late fees.** A return later than planned costs, per unit, ``extra_daily`` per started day of lateness when only a
daily fee is set, ``extra_hourly`` per started hour when only an hourly fee is set, and with both: ``extra_daily``
per full day plus ``extra_hourly`` per remaining started hour, the remaining hours costing at most one
``extra_daily``.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UNIT_HOURS = {"hour": 1, "day": 24, "week": 168, "month": 720}
# a rental is priced over at most five years (the dynamic programme is linear in the hours)
MAX_HOURS = 24 * 366 * 5


@dataclass(frozen=True)
class Block:
    """A pricing rule seen by the optimiser: ``hours`` of rental for ``price`` (``key`` identifies the rule)."""

    key: int
    hours: int
    price: float
    label: str


def zone(name: str | dt.tzinfo | None) -> dt.tzinfo:
    if isinstance(name, dt.tzinfo):
        return name
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return dt.UTC


def wall_clock_hours(start: dt.datetime, end: dt.datetime, tz: str | dt.tzinfo | None) -> int:
    """Started hours from ``start`` to ``end`` on the wall clock of ``tz`` (0 when ``end`` is not after
    ``start``)."""
    if end <= start:
        return 0
    z = zone(tz)
    s = start.astimezone(z).replace(tzinfo=None)
    e = end.astimezone(z).replace(tzinfo=None)
    seconds = (e - s).total_seconds()
    if seconds <= 0:  # the clocks went back over a very short rental: count the elapsed time instead
        seconds = (end - start).total_seconds()
    return max(1, math.ceil(seconds / 3600 - 1e-9))


def best_price(blocks: list[Block], hours: int) -> tuple[float, dict[int, int]]:
    """Cheapest cover of ``hours`` hours by ``blocks``: (price, {block key: count})."""
    usable = [b for b in blocks if b.hours > 0]
    if not usable or hours <= 0:
        return 0.0, {}
    if hours > MAX_HOURS:
        raise ValueError("rental too long")
    cost = [0.0] * (hours + 1)
    count = [0] * (hours + 1)
    choice: list[Block | None] = [None] * (hours + 1)
    for h in range(1, hours + 1):
        best: tuple[float, int] | None = None
        for block in usable:
            prev = max(0, h - block.hours)
            candidate = (round(cost[prev] + block.price, 6), count[prev] + 1)
            if best is None or candidate < best:
                best = candidate
                choice[h] = block
        assert best is not None
        cost[h], count[h] = best
    used: dict[int, int] = {}
    h = hours
    while h > 0:
        block = choice[h]
        assert block is not None
        used[block.key] = used.get(block.key, 0) + 1
        h = max(0, h - block.hours)
    return cost[hours], used


def describe(blocks: list[Block], used: dict[int, int]) -> str:
    """``2 x 1 week + 1 x 2 days`` (longest blocks first)."""
    by_key = {b.key: b for b in blocks}
    parts = sorted(used.items(), key=lambda kv: (-by_key[kv[0]].hours, kv[0]))
    return " + ".join(f"{n} x {by_key[k].label}" for k, n in parts)


def late_fee(hours_late: int, extra_hourly: float, extra_daily: float) -> float:
    """Late fee for one unit returned ``hours_late`` started hours late."""
    if hours_late <= 0:
        return 0.0
    hourly = float(extra_hourly or 0.0)
    daily = float(extra_daily or 0.0)
    if hourly and daily:
        days, rest = divmod(hours_late, 24)
        return days * daily + min(rest * hourly, daily)
    if daily:
        return math.ceil(hours_late / 24) * daily
    return hours_late * hourly
