"""RFC 5545 recurrence rules: validation and expansion into occurrences."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from dateutil.rrule import rrulestr

from tessera.exceptions import ValidationError

MAX_OCCURRENCES = 5000
SUPPORTED_FREQ = {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}


def normalize_rule(rule: str | None) -> str | None:
    if not rule:
        return None
    text = rule.strip().upper()
    if text.startswith("RRULE:"):
        text = text[6:]
    parts = dict(p.split("=", 1) for p in text.split(";") if "=" in p)
    freq = parts.get("FREQ", "").upper()
    if freq not in SUPPORTED_FREQ:
        raise ValidationError(f"Unsupported recurrence frequency '{freq or '?'}'", field="rrule")
    if "COUNT" in parts and "UNTIL" in parts:
        raise ValidationError("A recurrence has COUNT or UNTIL, not both", field="rrule")
    try:
        rrulestr(f"RRULE:{text}", dtstart=dt.datetime(2026, 1, 1, tzinfo=dt.UTC))
    except (ValueError, TypeError) as exc:
        raise ValidationError(f"Invalid recurrence rule: {exc}", field="rrule") from exc
    return text.upper()


def occurrences(
    start: dt.datetime,
    rule: str,
    window_start: dt.datetime,
    window_end: dt.datetime,
    exdates: Iterable[dt.datetime] = (),
) -> list[dt.datetime]:
    """Occurrence start times of a series in [window_start, window_end), excluding ``exdates``."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=dt.UTC)
    text = rule if rule.upper().startswith("RRULE:") else f"RRULE:{rule}"
    series = rrulestr(text, dtstart=start)
    excluded = {(e if e.tzinfo else e.replace(tzinfo=dt.UTC)).replace(microsecond=0) for e in exdates}
    out = []
    for occ in series.xafter(window_start - dt.timedelta(microseconds=1), count=MAX_OCCURRENCES, inc=True):
        if occ >= window_end:
            break
        if occ.replace(microsecond=0) not in excluded:
            out.append(occ)
    return out
