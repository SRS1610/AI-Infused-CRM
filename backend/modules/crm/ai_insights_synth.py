"""Synthetic CRM history with a *known* signal and noise, for the back-tests of the insight features.

The generating processes are written down here so the evaluation is honest: the models never see the
true parameters, the labels are drawn with noise, and the metrics (AUC, precision, MAPE, coverage,
accuracy) are computed on held-out draws. Used by ``ai/build_insights_evals.py`` (evaluation sets)
and by the evaluation entry points (training history), always with fixed seeds.
"""

from __future__ import annotations

import math
import random
from typing import Any

SOURCES = [("Website", 0.4), ("Referral", 0.15), ("Trade show", 0.2), (None, 0.25)]
TAGS = [("Hardware", 0.35), ("Services", 0.3), ("Upsell", 0.15)]
SOURCE_XMLID = {"Website": "crm.source_website", "Referral": "crm.source_referral", "Trade show": "crm.source_event"}
TAG_XMLID = {"Hardware": "crm.tag_hardware", "Services": "crm.tag_services", "Upsell": "crm.tag_upsell"}
TRAINING_SEED = 20261110
TRAINING_ROWS = 600


def _pick(rng: random.Random, options: list[tuple[Any, float]]) -> Any:
    r, acc = rng.random(), 0.0
    for value, p in options:
        acc += p
        if r < acc:
            return value
    return options[-1][0]


def _poisson(rng: random.Random, lam: float) -> int:
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def true_lead_logit(v: dict[str, Any]) -> float:
    """The ground truth the model has to discover (it never sees these weights)."""
    free = str(v.get("email_from") or "").endswith(("@gmail.com", "@outlook.com"))
    return (
        -3.6
        + 1.7 * bool(v["has_company"])
        + 1.5 * bool(v["existing_customer"])
        + 1.1 * bool(v["has_phone"])
        - 1.0 * free
        + {"Referral": 2.5, "Trade show": 1.1, "Website": 0.0, None: -0.9}[v["source"]]
        + 0.75 * int(v["priority"])
        + 1.1 * math.log1p(v["messages"])
        - 0.4 * (math.log1p(v["expected_revenue"]) - math.log(15000))
        + 0.9 * ("Upsell" in v["tags"])
    )


def synthetic_lead(rng: random.Random, n: int) -> tuple[dict[str, Any], int]:
    """One lead's normalised values (the shape ``ai_insights.lead_values`` returns) and its outcome."""
    has_company = rng.random() < 0.65
    email_kind = _pick(rng, [("business", 0.6), ("free", 0.3), (None, 0.1)])
    v: dict[str, Any] = {
        "expected_revenue": round(math.exp(rng.gauss(math.log(15000), 1.0)), -2),
        "has_company": has_company,
        "existing_customer": rng.random() < (0.3 if has_company else 0.05),
        "has_phone": rng.random() < 0.55,
        "email_from": {"business": f"buyer@company{n}.example", "free": f"buyer{n}@gmail.com", None: None}[email_kind],
        "website": f"https://company{n}.example" if rng.random() < (0.4 if has_company else 0.1) else None,
        "priority": _pick(rng, [(0, 0.45), (1, 0.3), (2, 0.15), (3, 0.1)]),
        "source": _pick(rng, SOURCES),
        "tags": [t for t, p in TAGS if rng.random() < p],
    }
    v["messages"] = _poisson(rng, 1.5 + 1.0 * has_company)
    logit = true_lead_logit(v) + rng.gauss(0, 0.6)  # unobserved factors
    won = 1 if rng.random() < 1 / (1 + math.exp(-logit)) else 0
    return v, won


def lead_history(seed: int = TRAINING_SEED, n: int = TRAINING_ROWS) -> tuple[list[dict[str, Any]], list[int]]:
    rng = random.Random(seed)  # noqa: S311 - deterministic synthetic data
    rows, labels = [], []
    for i in range(n):
        v, y = synthetic_lead(rng, 10_000 + i)
        rows.append(v)
        labels.append(y)
    return rows, labels


# ----------------------------------------------------------------------------- deal risk


def synthetic_deal(rng: random.Random) -> tuple[dict[str, Any], bool]:
    """Signals of an open opportunity and whether it slipped or was lost in the following 30 days."""
    healthy = rng.random() < 0.6
    s: dict[str, Any] = {
        "days_since_activity": int(rng.expovariate(1 / (6 if healthy else 24))) + 1,
        "stage_age_days": int(rng.expovariate(1 / (20 if healthy else 55))) + 1,
        "has_next_activity": rng.random() < (0.8 if healthy else 0.4),
        "days_past_close": rng.randint(-60, -5) if rng.random() < (0.9 if healthy else 0.55) else rng.randint(1, 40),
        "negative": rng.random() < (0.05 if healthy else 0.3),
    }
    s["next_activity_overdue_days"] = (
        (rng.randint(1, 20) if rng.random() < (0.15 if healthy else 0.45) else 0) if s["has_next_activity"] else 0
    )
    logit = (
        -3.2
        + 0.09 * max(0, s["days_since_activity"] - 10)
        + 0.035 * max(0, s["stage_age_days"] - 30)
        + 1.5 * (s["next_activity_overdue_days"] > 0)
        + 1.0 * (not s["has_next_activity"])
        + 1.7 * (s["days_past_close"] > 0)
        + 2.4 * s["negative"]
        + rng.gauss(0, 0.5)
    )
    return s, rng.random() < 1 / (1 + math.exp(-logit))


# ----------------------------------------------------------------------------- forecast

STAGES = [("Qualified", 0.3), ("Proposition", 0.6), ("Negotiation", 0.8)]  # name, default probability


def forecast_scenario(rng: random.Random, case: int) -> tuple[dict[str, Any], float]:
    """A company-month: open pipeline, decaying history and the revenue actually won that month."""
    base = [0.22, 0.42, 0.68]
    theta_hist = [min(0.95, max(0.03, b + rng.gauss(0, 0.06))) for b in base]
    theta_now = [min(0.95, max(0.03, t + rng.gauss(0, 0.03))) for t in theta_hist]  # drift
    slip_true = rng.uniform(0.04, 0.18)
    run_rate_mean = rng.uniform(5_000, 40_000)
    deal_size = rng.uniform(12_000, 35_000)
    n_deals = rng.randint(70, 140)
    pipeline = []
    for i in range(n_deals):
        st = rng.choices(range(3), weights=[0.45, 0.35, 0.2])[0]
        pipeline.append(
            {
                "id": i + 1,
                "name": f"Deal {case}-{i + 1}",
                "amount": round(math.exp(rng.gauss(math.log(deal_size), 0.5)), -2),
                "stage": STAGES[st][0],
                "period": 0,
            }
        )
    stage_history: dict[str, Any] = {}
    for st, (name, prior) in enumerate(STAGES):
        months = []
        for age in range(1, 13):
            closed = rng.randint(3, 9)
            won = sum(1 for _ in range(closed) if rng.random() < theta_hist[st])
            months.append({"age_months": age, "won": won, "lost": closed - won})
        stage_history[name] = {"prior": prior, "months": months}
    slip_history = []
    for age in range(1, 13):
        won = rng.randint(4, 12)
        slipped = sum(1 for _ in range(won) if rng.random() < slip_true)
        slip_history.append({"age_months": age, "slipped": slipped, "on_time": won - slipped})
    shape = 4.0
    run_rate_history = [round(rng.gammavariate(shape, run_rate_mean / shape), -2) for _ in range(12)]
    committed = round(rng.uniform(0, 0.15) * n_deals * deal_size * 0.3, -2)
    # what actually happened
    actual = committed + rng.gammavariate(shape, run_rate_mean / shape)
    for d in pipeline:
        st = [s[0] for s in STAGES].index(d["stage"])
        if rng.random() < theta_now[st] and rng.random() >= slip_true:
            actual += d["amount"]
    inputs = {
        "pipeline": pipeline,
        "stage_history": stage_history,
        "slip_history": slip_history,
        "run_rate_history": run_rate_history,
        "committed": [committed],
    }
    return inputs, round(actual, 2)
