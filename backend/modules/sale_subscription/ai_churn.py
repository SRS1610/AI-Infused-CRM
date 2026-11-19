"""Numerical core of ``sale_subscription.churn_predict`` (no ORM, no LLM).

A logistic regression on standardised features computed from :func:`modules.sale_subscription.metrics.churn_features`.
In log-odds space each factor's contribution is exact: ``coef x (value - training mean) / scale``, so removing a
factor (setting it to the average subscription's value) moves the probability the stated way by the stated weight.

* **Company model.** Trained nightly per company on its ended and surviving subscriptions
  (:func:`fit_churn_model`), once it has at least :data:`MIN_TRAINING_ROWS` of them with
  :data:`MIN_PER_CLASS` in each class; a stratified hold-out AUC is stored with the artefact (JSON, no pickle).
* **Cold start.** Before that, :data:`PRIOR` — documented weights on the same standardised features — scores
  every subscription, with the same explanations.
* **Back-test.** :func:`synthetic_subscription` draws subscriptions with a known churn signal plus noise; the
  evaluation trains on one seed and scores held-out subscriptions created in the database (``ai/build_evals.py``).

The model never sees the subscription's state, close reason, customer identity or any person's attributes: only
:data:`FEATURES`, which :func:`vector` reads from the churn facts.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from tessera.i18n import _

ALGORITHM = "logistic_regression"
MIN_TRAINING_ROWS = 40
MIN_PER_CLASS = 10
RISK_MEDIUM = 0.35  # probability from which a subscription is "at risk"
RISK_HIGH = 0.6
TOP_FACTORS = 5


@dataclass(frozen=True)
class Feature:
    name: str
    binary: bool = False


FEATURES: tuple[Feature, ...] = (
    Feature("log_tenure"),
    Feature("log_mrr"),
    Feature("products"),
    Feature("discount"),
    Feature("fixed_term", binary=True),
    Feature("renewals"),
    Feature("upsells"),
    Feature("downsells"),
    Feature("mrr_trend"),
    Feature("pauses"),
    Feature("paused", binary=True),
    Feature("failures"),
    Feature("no_payment_method", binary=True),
    Feature("unpaid"),
    Feature("log_overdue"),
    Feature("messages"),
    Feature("reactivations"),
)
NAMES = [f.name for f in FEATURES]
#: churn facts the model reads (everything else in ``churn_features`` is out of its reach)
INPUTS = frozenset(
    {
        "tenure_days",
        "mrr_company",
        "product_count",
        "average_discount",
        "fixed_term",
        "renewals",
        "upsell_count",
        "downsell_count",
        "mrr_change_90d",
        "pause_count",
        "paused",
        "payment_failures",
        "has_payment_method",
        "unpaid_invoice_count",
        "overdue_amount",
        "customer_messages",
        "reactivation_count",
    }
)
#: facts that must never be inputs: the outcome itself and who the customer is
EXCLUDED = frozenset({"state", "churned", "close_reason", "partner_id", "name", "subscription_id", "plan"})


def raw_features(facts: dict[str, Any]) -> dict[str, float]:
    """Model inputs from the churn facts of one subscription."""
    g = facts.get
    mrr = max(float(g("mrr_company") or 0.0), 0.0)
    trend = float(g("mrr_change_90d") or 0.0) / max(mrr, 1.0)
    return {
        "log_tenure": math.log1p(max(float(g("tenure_days") or 0) / 30.4375, 0.0)),
        "log_mrr": math.log1p(mrr),
        "products": float(g("product_count") or 0),
        "discount": float(g("average_discount") or 0.0),
        "fixed_term": 1.0 if g("fixed_term") else 0.0,
        "renewals": float(g("renewals") or 0),
        "upsells": float(g("upsell_count") or 0),
        "downsells": float(g("downsell_count") or 0),
        "mrr_trend": max(-1.0, min(1.0, trend)),
        "pauses": float(g("pause_count") or 0),
        "paused": 1.0 if g("paused") else 0.0,
        "failures": float(g("payment_failures") or 0),
        "no_payment_method": 0.0 if g("has_payment_method") else 1.0,
        "unpaid": float(g("unpaid_invoice_count") or 0),
        "log_overdue": math.log1p(max(float(g("overdue_amount") or 0.0), 0.0)),
        "messages": float(g("customer_messages") or 0),
        "reactivations": float(g("reactivation_count") or 0),
    }


def vector(facts: dict[str, Any], names: Sequence[str] = NAMES) -> np.ndarray:
    base = raw_features(facts)
    return np.array([base[n] for n in names], dtype=float)


def label(feature: str, facts: dict[str, Any], currency: str = "") -> str:
    """A factor in words, with the subscription's own value (shown in the user's language)."""
    g = facts.get
    cur = f" {currency}" if currency else ""
    n = int(g({"products": "product_count", "renewals": "renewals", "upsells": "upsell_count",
               "downsells": "downsell_count", "pauses": "pause_count", "failures": "payment_failures",
               "unpaid": "unpaid_invoice_count", "messages": "customer_messages",
               "reactivations": "reactivation_count"}.get(feature, "")) or 0)  # fmt: skip
    months = round(float(g("tenure_days") or 0) / 30.4375, 1)
    labels = {
        "log_tenure": _("Customer for {months} months", months=months),
        "log_mrr": _("Monthly recurring revenue {amount}", amount=f"{float(g('mrr_company') or 0):,.2f}{cur}"),
        "products": _("{count} subscribed products", count=n),
        "discount": _("Average discount {percent} %", percent=f"{float(g('average_discount') or 0):g}"),
        "fixed_term": _("Fixed term") if g("fixed_term") else _("Open-ended"),
        "renewals": _("Renewed {count} times", count=n),
        "upsells": _("{count} upgrades", count=n),
        "downsells": _("{count} downgrades", count=n),
        "mrr_trend": _("MRR change over 90 days: {amount}", amount=f"{float(g('mrr_change_90d') or 0):+,.2f}{cur}"),
        "pauses": _("Paused {count} times", count=n),
        "paused": _("Paused now") if g("paused") else _("Not paused"),
        "failures": _("{count} failed payments", count=n),
        "no_payment_method": _("Saved payment method") if g("has_payment_method") else _("No saved payment method"),
        "unpaid": _("{count} unpaid invoices", count=n),
        "log_overdue": _("Overdue {amount}", amount=f"{float(g('overdue_amount') or 0):,.2f}{cur}"),
        "messages": _("{count} e-mails from the customer", count=n),
        "reactivations": _("Reactivated {count} times", count=n),
    }
    return str(labels[feature])


# ----------------------------------------------------------------------------------------------- the prior

# Cold-start weights (log-odds per standard deviation, on reference means / scales of a typical book of
# subscriptions): payment trouble, pauses, downgrades and a falling MRR raise the risk; tenure, renewals,
# upgrades and a saved payment method lower it. Replaced by the company's own model once it has the history.
_PRIOR_SPEC = {
    "log_tenure": (2.6, 0.9, -0.55),
    "log_mrr": (5.5, 1.3, -0.1),
    "products": (1.6, 0.8, -0.1),
    "discount": (5.0, 8.0, 0.15),
    "fixed_term": (0.3, 0.46, -0.1),
    "renewals": (0.3, 0.7, -0.35),
    "upsells": (0.4, 0.7, -0.3),
    "downsells": (0.3, 0.6, 0.45),
    "mrr_trend": (0.0, 0.2, -0.35),
    "pauses": (0.3, 0.6, 0.4),
    "paused": (0.1, 0.3, 0.5),
    "failures": (0.6, 1.0, 0.75),
    "no_payment_method": (0.5, 0.5, 0.2),
    "unpaid": (0.3, 0.7, 0.4),
    "log_overdue": (0.8, 2.0, 0.35),
    "messages": (1.5, 1.6, 0.2),
    "reactivations": (0.05, 0.25, 0.2),
}


def _prior() -> dict[str, Any]:
    art: dict[str, Any] = {
        "algorithm": "prior",
        "features": NAMES,
        "mean": [_PRIOR_SPEC[n][0] for n in NAMES],
        "scale": [_PRIOR_SPEC[n][1] for n in NAMES],
        "coef": [_PRIOR_SPEC[n][2] for n in NAMES],
        "intercept": -1.4,
        "metrics": {},
    }
    art["hash"] = artefact_hash(art)
    return art


def artefact_hash(artefact: dict[str, Any]) -> str:
    body = {k: v for k, v in artefact.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


PRIOR: dict[str, Any] = {}


# ----------------------------------------------------------------------------------------------- training


def auc(labels: Sequence[int], scores: Sequence[float]) -> float:
    """Area under the ROC curve (Mann-Whitney U, average ranks for ties)."""
    y = np.asarray(labels, dtype=int)
    s = np.asarray(scores, dtype=float)
    pos, neg = int(y.sum()), int(len(y) - y.sum())
    if pos == 0 or neg == 0:
        return float("nan")
    order = s.argsort(kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    ordered = s[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and ordered[j + 1] == ordered[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def fit_churn_model(rows: list[dict[str, Any]], labels: list[int], *, seed: int = 0) -> dict[str, Any]:
    """Train on churn facts (``rows``) and outcomes (1 = churned). JSON artefact with a stratified hold-out AUC."""
    from sklearn.linear_model import LogisticRegression

    if len(rows) != len(labels):
        raise ValueError("rows and labels differ in length")
    y = np.asarray(labels, dtype=int)
    if len(rows) < MIN_TRAINING_ROWS or y.sum() < MIN_PER_CLASS or (len(y) - y.sum()) < MIN_PER_CLASS:
        raise ValueError("not enough ended and surviving subscriptions to train")
    X = np.vstack([vector(r) for r in rows])  # noqa: N806
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale < 1e-9] = 1.0

    def train(idx: np.ndarray) -> Any:
        model = LogisticRegression(C=1.0, max_iter=2000)
        model.fit((X[idx] - mean) / scale, y[idx])
        return model

    rng = np.random.default_rng(seed)
    hold: list[int] = []
    for cls in (0, 1):
        members = np.flatnonzero(y == cls)
        rng.shuffle(members)
        hold += list(members[: max(1, len(members) // 5)])
    holdout = np.array(sorted(hold))
    check = train(np.setdiff1d(np.arange(len(y)), holdout))
    holdout_auc = auc(list(y[holdout]), list(check.predict_proba((X[holdout] - mean) / scale)[:, 1]))
    final = train(np.arange(len(y)))
    art: dict[str, Any] = {
        "algorithm": ALGORITHM,
        "features": NAMES,
        "mean": [round(float(v), 8) for v in mean],
        "scale": [round(float(v), 8) for v in scale],
        "coef": [round(float(v), 8) for v in final.coef_[0]],
        "intercept": round(float(final.intercept_[0]), 8),
        "metrics": {
            "rows": len(y),
            "churned": int(y.sum()),
            "base_rate": round(float(y.mean()), 4),
            "holdout_auc": round(holdout_auc, 4),
            "holdout_rows": len(holdout),
        },
    }
    art["hash"] = artefact_hash(art)
    return art


def risk_level(probability: float) -> str:
    return "high" if probability >= RISK_HIGH else "medium" if probability >= RISK_MEDIUM else "low"


def predict(artefact: dict[str, Any], facts: dict[str, Any], top: int = TOP_FACTORS) -> dict[str, Any]:
    """Churn probability, 0-100 score, risk level and the top contributing factors
    (``{feature, value, direction, weight}``; weight = contribution to the log-odds; positive raises the risk)."""
    names = artefact["features"]
    x = vector(facts, names)
    z = (x - np.asarray(artefact["mean"])) / np.asarray(artefact["scale"])
    contrib = np.asarray(artefact["coef"]) * z
    logit = float(artefact["intercept"] + contrib.sum())
    prob = 1.0 / (1.0 + math.exp(-logit))
    order = sorted(range(len(names)), key=lambda i: (-abs(contrib[i]), names[i]))
    factors = [
        {
            "feature": names[i],
            "value": round(float(x[i]), 4),
            "direction": "raises" if contrib[i] > 0 else "lowers",
            "weight": round(float(contrib[i]), 4),
        }
        for i in order[:top]
        if abs(contrib[i]) >= 1e-6
    ]
    return {
        "probability": round(prob, 4),
        "score": round(prob * 100),
        "risk": risk_level(prob),
        "factors": factors,
        "algorithm": artefact.get("algorithm", ALGORITHM),
    }


def factor_validity(artefact: dict[str, Any], facts: dict[str, Any], prediction: dict[str, Any]) -> float:
    """Share of the explanation's factors that hold up: a model input with the subscription's actual value, and
    neutralising it (training mean) moves the log-odds against the stated direction by exactly the stated weight."""
    names = artefact["features"]
    if not prediction["factors"]:
        return 1.0
    x = vector(facts, names)
    mean, scale, coef = (np.asarray(artefact[k]) for k in ("mean", "scale", "coef"))
    base = float(artefact["intercept"] + (coef * (x - mean) / scale).sum())
    ok = 0
    for f in prediction["factors"]:
        if f["feature"] not in names:
            continue
        i = names.index(f["feature"])
        if abs(float(x[i]) - f["value"]) > 1e-3:
            continue
        ablated = x.copy()
        ablated[i] = mean[i]
        moved = base - float(artefact["intercept"] + (coef * (ablated - mean) / scale).sum())
        if (moved > 0) == (f["direction"] == "raises") and abs(moved - f["weight"]) < 1e-3:
            ok += 1
    return ok / len(prediction["factors"])


# ----------------------------------------------------------------------------------------------- synthetic

TRAINING_SEED = 4242
EVAL_SEED = 1717
PRICES = (19.0, 49.0, 99.0, 199.0, 450.0, 1200.0)


def synthetic_subscription(rng: random.Random) -> tuple[dict[str, Any], int]:
    """One subscription's churn facts drawn with a known signal, and its outcome (1 = churned in the next period).

    The facts are those the evaluation can create as records (lines, MRR movements, pauses, failed payments,
    e-mails); invoices, saved cards and renewals are left at their defaults."""
    tenure_days = int(rng.randint(1, 48) * 30.4375)
    products = rng.choice([1, 1, 1, 2, 2, 3])
    lines = [
        {"price": rng.choice(PRICES), "quantity": rng.randint(1, 8), "discount": rng.choice([0, 0, 0, 10, 15, 25])}
        for _ in range(products)
    ]
    mrr = round(sum(li["price"] * li["quantity"] * (1 - li["discount"] / 100) for li in lines), 2)
    events = []
    for kind, n in (("upsell", rng.choice([0, 0, 0, 1, 1, 2])), ("downsell", rng.choice([0, 0, 0, 0, 1, 2]))):
        for _k in range(n):
            amount = round(rng.uniform(20, 300), 2) * (1 if kind == "upsell" else -1)
            events.append(
                {"kind": kind, "days_ago": rng.randint(5, max(6, min(400, tenure_days - 1))), "amount": amount}
            )
    reactivated = 1 if rng.random() < 0.06 else 0
    paused = rng.random() < 0.1
    pauses = (1 if paused else 0) + rng.choice([0, 0, 0, 1, 2])
    failures = rng.choice([0, 0, 0, 0, 0, 1, 1, 2, 3, 4])
    messages = rng.choice([0, 0, 1, 1, 2, 3, 4, 6])
    fixed = rng.random() < 0.3
    facts = {
        "tenure_days": tenure_days,
        "mrr_company": mrr,
        "product_count": products,
        "line_count": products,
        "average_discount": round(sum(li["discount"] for li in lines) / len(lines), 2),
        "fixed_term": fixed,
        "renewals": 0,
        "upsell_count": sum(1 for e in events if e["kind"] == "upsell"),
        "downsell_count": sum(1 for e in events if e["kind"] == "downsell"),
        "mrr_change_90d": round(sum(e["amount"] for e in events if e["days_ago"] <= 90), 2),
        "pause_count": pauses,
        "paused": paused,
        "payment_failures": failures,
        "has_payment_method": False,
        "unpaid_invoice_count": 0,
        "overdue_amount": 0.0,
        "customer_messages": messages,
        "reactivation_count": reactivated,
        "_lines": lines,
        "_events": events,
    }
    raw = raw_features(facts)
    logit = (
        -2.1
        - 0.85 * (raw["log_tenure"] - 2.6)
        + 0.75 * failures
        + 0.55 * (pauses - 0.3)
        + 1.1 * raw["paused"]
        + 0.8 * facts["downsell_count"]
        - 0.55 * facts["upsell_count"]
        - 1.2 * raw["mrr_trend"]
        + 0.22 * messages
        + 0.03 * facts["average_discount"]
        + 0.6 * reactivated
        - 0.12 * (raw["log_mrr"] - 5.5)
        + rng.gauss(0.0, 0.7)
    )
    return facts, int(rng.random() < 1.0 / (1.0 + math.exp(-logit)))


def synthetic_history(n: int = 600, seed: int = TRAINING_SEED) -> tuple[list[dict[str, Any]], list[int]]:
    rng = random.Random(seed)  # noqa: S311 - deterministic synthetic data
    rows, labels = [], []
    for _k in range(n):
        facts, churned = synthetic_subscription(rng)
        rows.append(facts)
        labels.append(churned)
    return rows, labels


PRIOR.update(_prior())
