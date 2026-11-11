"""Synthetic order history for the ``sale.upsell`` back-test (deterministic, seeded).

A catalogue of 30 products sold in eleven typical "bundles" (a robot cell, a vision kit, a workstation, a
packaging line…), each product of a bundle bought with its own probability, plus noise: a random product in
one order out of five and a product of another bundle in one out of ten. Orders run over 24 months; the
back-test trains on the first 18 and tests on the last 6. After the cut-off the market drifts (vision kits
start selling with robot arms), so the test period is not a copy of the training one.
"""

from __future__ import annotations

import datetime as dt
import random
from functools import lru_cache

SEED = 20261110
START = dt.date(2025, 1, 1)
MONTHS = 24
CUTOFF = dt.date(2026, 7, 1)
ORDERS_PER_MONTH = 55

CATALOGUE = [
    "ARM", "GRIP", "FTS", "CTRL", "MOUNT", "FENCE", "SCAN", "CAM", "VISW", "CABLE", "ONB", "TRAIN", "SUP", "CONS",
    "SPARE", "LAPTOP", "DOCK", "MON", "KEYB", "MOUSE", "HEAD", "BAG", "WARR", "DESK", "CHAIR", "LAMP", "CONV",
    "LABEL", "RIBBON", "SCALE",
]  # fmt: skip

BUNDLES: list[tuple[str, float, dict[str, float]]] = [
    ("robot_cell", 3.0, {"ARM": 0.95, "GRIP": 0.8, "CTRL": 0.7, "MOUNT": 0.6, "FENCE": 0.35, "ONB": 0.45}),
    ("safety", 1.2, {"FENCE": 0.9, "SCAN": 0.85, "CONS": 0.35}),
    ("vision", 1.5, {"CAM": 0.9, "VISW": 0.85, "CABLE": 0.5, "CONS": 0.25}),
    ("services", 1.5, {"ONB": 0.7, "TRAIN": 0.7, "SUP": 0.6}),
    ("sensing", 1.0, {"FTS": 0.9, "GRIP": 0.45, "CABLE": 0.6}),
    ("workstation", 2.5, {"LAPTOP": 0.95, "DOCK": 0.7, "MON": 0.7, "KEYB": 0.5, "MOUSE": 0.5, "WARR": 0.45}),
    ("remote", 1.2, {"LAPTOP": 0.8, "HEAD": 0.8, "BAG": 0.7, "WARR": 0.35}),
    ("furniture", 1.3, {"DESK": 0.9, "CHAIR": 0.9, "LAMP": 0.6, "MON": 0.25}),
    ("packaging", 1.4, {"CONV": 0.9, "LABEL": 0.7, "RIBBON": 0.7, "SCALE": 0.5, "CONS": 0.2}),
    ("labels", 0.8, {"LABEL": 0.35, "RIBBON": 0.95}),
    ("maintenance", 1.0, {"SPARE": 0.9, "SUP": 0.5, "CONS": 0.4, "CABLE": 0.3}),
]


def _month(i: int) -> dt.date:
    return dt.date(START.year + (START.month - 1 + i) // 12, (START.month - 1 + i) % 12 + 1, 1)


def _basket(rng: random.Random, when: dt.date) -> list[str]:
    weights = [w for _, w, _ in BUNDLES]
    name, _w, members = rng.choices(BUNDLES, weights=weights)[0]
    members = dict(members)
    if name == "vision" and when >= CUTOFF:  # drift: vision-guided picking cells
        members["ARM"] = 0.5
    items = [p for p, prob in members.items() if rng.random() < prob]
    if not items:
        items = [max(members, key=lambda p: members[p])]
    if rng.random() < 0.2:
        items.append(rng.choice(CATALOGUE))
    if rng.random() < 0.1:
        other = rng.choice(BUNDLES)[2]
        items.append(max(other, key=lambda p: other[p]))
    return list(dict.fromkeys(items))


@lru_cache(maxsize=1)
def history() -> tuple[tuple[dt.date, tuple[str, ...]], ...]:
    """(order date, products) for every order of the 24 months, in date order."""
    rng = random.Random(SEED)
    out: list[tuple[dt.date, tuple[str, ...]]] = []
    for m in range(MONTHS):
        first = _month(m)
        days = (_month(m + 1) - first).days
        for _ in range(ORDERS_PER_MONTH):
            when = first + dt.timedelta(days=rng.randrange(days))
            out.append((when, tuple(_basket(rng, when))))
    out.sort(key=lambda o: o[0])
    return tuple(out)


def training_baskets(cutoff: dt.date = CUTOFF) -> list[tuple[str, ...]]:
    return [items for when, items in history() if when < cutoff]


def test_cases(count: int = 120, cutoff: dt.date = CUTOFF) -> list[dict[str, object]]:
    """Orders after the cut-off with at least two products: one product hidden, the rest is the basket."""
    rng = random.Random(SEED + 1)
    cases: list[dict[str, object]] = []
    for when, items in history():
        if when < cutoff or len(items) < 2:
            continue
        hidden = rng.choice(items)
        basket = [p for p in items if p != hidden]
        cases.append({"date": when.isoformat(), "basket": basket, "hidden": hidden})
        if len(cases) >= count:
            break
    return cases
