"""Numerical core of the CRM insight features (AI architecture note §4.7): no ORM, no LLM.

* Lead scoring: a per-company logistic regression on standardised features. A linear model in log-odds
  space makes the explanation exact: each factor's weight is its contribution to the log-odds relative
  to the average lead of the training set, so removing it moves the probability the stated way.
* Deal risk: transparent weighted signals (stale activity, stage age, overdue work, passed closing
  date, negative wording in recent messages).
* Forecast: Monte-Carlo simulation of the open pipeline with Beta posteriors on stage win rates and
  slippage (history down-weighted with a half-life, because win rates drift), plus a bootstrapped
  run-rate of deals that are created and won within one period.

The same functions serve the scheduled jobs (rows extracted from the database) and the back-tests
(synthetic rows from :mod:`modules.crm.ai_insights_synth`).
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

# ----------------------------------------------------------------------------- lead scoring features

ALGORITHM = "logistic_regression"
MIN_TRAINING_ROWS = 40  # closed deals needed before a company gets its own model (cold start → rules)
MIN_PER_CLASS = 10
MAX_VOCAB = 8
MIN_VOCAB_COUNT = 5
FREE_MAIL = {"gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "gmx.de", "web.de", "icloud.com", "mail.example"}

#: Lead fields a scoring feature may be computed from. Everything else is out of reach of the model.
ALLOWED_SOURCE_FIELDS = frozenset(
    {
        "expected_revenue",
        "partner_name",
        "partner_id",
        "phone",
        "mobile",
        "email_from",
        "website",
        "priority",
        "source_id",
        "tag_ids",
        "chatter.message",  # number of logged conversations (engagement), not their content
    }
)

#: Protected attributes of a *person* (1.6-d) and the proxies identified in review. None of them may
#: ever be a model input; ``assert_allowed`` enforces it at extraction time and a test asserts the
#: feature list.
PROTECTED_AND_PROXIES = frozenset(
    {
        # protected attributes
        "gender",
        "sex",
        "age",
        "birthdate",
        "date_of_birth",
        "ethnicity",
        "race",
        "religion",
        "disability",
        "nationality",
        # proxies: names and salutations (gender, ethnicity), location of the person (ethnicity,
        # nationality), language and phone prefixes (nationality), photos
        "title",
        "contact_name",
        "name",
        "lang",
        "country_id",
        "city",
        "zip",
        "street",
        "state_id",
        "phone_prefix",
        "image",
        "function",
    }
)


@dataclass(frozen=True)
class FeatureDef:
    name: str
    sources: tuple[str, ...]
    label_on: str  # label when the value pushes the score (binary: value 1)
    label_off: str = ""  # binary features: label when the value is 0


BASE_FEATURES: tuple[FeatureDef, ...] = (
    FeatureDef("log_revenue", ("expected_revenue",), "Expected revenue"),
    FeatureDef("has_company", ("partner_name", "partner_id"), "Company known", "No company name"),
    FeatureDef("existing_customer", ("partner_id",), "Linked to an existing contact", "Not linked to a contact"),
    FeatureDef("has_phone", ("phone", "mobile"), "Phone number given", "No phone number"),
    FeatureDef("has_email", ("email_from",), "Email address given", "No email address"),
    FeatureDef("free_email", ("email_from",), "Free email provider", "Business email domain"),
    FeatureDef("has_website", ("website",), "Website given", "No website"),
    FeatureDef("priority", ("priority",), "Priority"),
    FeatureDef("log_messages", ("chatter.message",), "Logged conversations"),
    FeatureDef("n_tags", ("tag_ids",), "Number of tags"),
)
CATEGORICAL = {"source": (("source_id",), "Source"), "tag": (("tag_ids",), "Tag")}
BINARY = {"has_company", "existing_customer", "has_phone", "has_email", "free_email", "has_website"}


class ProtectedAttributeError(AssertionError):
    pass


def assert_allowed(field: str) -> str:
    """Guard every read of the feature extractor: only allow-listed, non-protected lead fields."""
    if field in PROTECTED_AND_PROXIES or field not in ALLOWED_SOURCE_FIELDS:
        raise ProtectedAttributeError(f"'{field}' may not be used as a scoring input")
    return field


def feature_definitions(vocab: dict[str, list[str]] | None = None) -> list[FeatureDef]:
    """Base features plus the one-hot categorical features of a trained vocabulary."""
    out = list(BASE_FEATURES)
    for kind, values in (vocab or {}).items():
        sources, label = CATEGORICAL[kind]
        out += [FeatureDef(f"{kind}={v}", sources, f"{label}: {v}", f"{label} not {v}") for v in values]
    return out


def raw_features(values: dict[str, Any]) -> dict[str, float]:
    """Numeric base features from normalised lead values (see ``ai_insights.lead_values``)."""
    email = str(values.get("email_from") or "")
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    return {
        "log_revenue": math.log1p(max(float(values.get("expected_revenue") or 0.0), 0.0)),
        "has_company": 1.0 if values.get("has_company") else 0.0,
        "existing_customer": 1.0 if values.get("existing_customer") else 0.0,
        "has_phone": 1.0 if values.get("has_phone") else 0.0,
        "has_email": 1.0 if email else 0.0,
        "free_email": 1.0 if domain in FREE_MAIL else 0.0,
        "has_website": 1.0 if values.get("website") else 0.0,
        "priority": float(int(values.get("priority") or 0)),
        "log_messages": math.log1p(max(int(values.get("messages") or 0), 0)),
        "n_tags": float(len(values.get("tags") or [])),
    }


def _vector(values: dict[str, Any], names: Sequence[str]) -> np.ndarray:
    base = raw_features(values)
    tags = set(values.get("tags") or [])
    out = []
    for n in names:
        if n.startswith("source="):
            out.append(1.0 if values.get("source") == n[7:] else 0.0)
        elif n.startswith("tag="):
            out.append(1.0 if n[4:] in tags else 0.0)
        else:
            out.append(base[n])
    return np.array(out, dtype=float)


def build_vocab(rows: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    sources: Counter[str] = Counter()
    tags: Counter[str] = Counter()
    for r in rows:
        if r.get("source"):
            sources[str(r["source"])] += 1
        tags.update(str(t) for t in r.get("tags") or [])
    pick = lambda c: sorted(k for k, n in c.most_common(MAX_VOCAB) if n >= MIN_VOCAB_COUNT)  # noqa: E731
    return {"source": pick(sources), "tag": pick(tags)}


def auc(labels: Sequence[int], scores: Sequence[float]) -> float:
    """Area under the ROC curve (Mann-Whitney U with average ranks for ties)."""
    y = np.asarray(labels, dtype=int)
    s = np.asarray(scores, dtype=float)
    pos, neg = int(y.sum()), int(len(y) - y.sum())
    if pos == 0 or neg == 0:
        return float("nan")
    order = s.argsort(kind="mergesort")
    ranks = np.empty(len(s), dtype=float)
    sorted_s = s[order]
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and sorted_s[j + 1] == sorted_s[i]:
            j += 1
        ranks[order[i : j + 1]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return float((ranks[y == 1].sum() - pos * (pos + 1) / 2.0) / (pos * neg))


def fit_lead_model(rows: list[dict[str, Any]], labels: list[int], *, seed: int = 0) -> dict[str, Any]:
    """Train the scoring model. Returns a JSON artefact (no pickle): standardisation, coefficients,
    vocabulary and training metadata, including a stratified hold-out AUC."""
    from sklearn.linear_model import LogisticRegression

    if len(rows) != len(labels):
        raise ValueError("rows and labels differ in length")
    y = np.asarray(labels, dtype=int)
    if len(rows) < MIN_TRAINING_ROWS or y.sum() < MIN_PER_CLASS or (len(y) - y.sum()) < MIN_PER_CLASS:
        raise ValueError("not enough closed deals to train")
    vocab = build_vocab(rows)
    names = [f.name for f in feature_definitions(vocab)]
    X = np.vstack([_vector(r, names) for r in rows])  # noqa: N806
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
    train_idx = np.setdiff1d(np.arange(len(y)), holdout)
    check = train(train_idx)
    holdout_auc = auc(y[holdout], check.predict_proba((X[holdout] - mean) / scale)[:, 1])
    final = train(np.arange(len(y)))
    artefact: dict[str, Any] = {
        "algorithm": ALGORITHM,
        "features": names,
        "mean": [round(float(v), 8) for v in mean],
        "scale": [round(float(v), 8) for v in scale],
        "coef": [round(float(v), 8) for v in final.coef_[0]],
        "intercept": round(float(final.intercept_[0]), 8),
        "vocab": vocab,
        "metrics": {
            "rows": len(y),
            "won": int(y.sum()),
            "lost": int(len(y) - y.sum()),
            "base_rate": round(float(y.mean()), 4),
            "holdout_auc": round(holdout_auc, 4),
            "holdout_rows": len(holdout),
        },
    }
    artefact["hash"] = artefact_hash(artefact)
    return artefact


def artefact_hash(artefact: dict[str, Any]) -> str:
    body = {k: v for k, v in artefact.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def _label(fdef: FeatureDef, value: float, raw_value: Any) -> str:
    if fdef.name in BINARY or "=" in fdef.name:
        return fdef.label_on if value >= 0.5 else fdef.label_off
    if fdef.name == "log_revenue":
        return f"Expected revenue {float(raw_value or 0):,.0f}"
    if fdef.name == "log_messages":
        return f"{int(raw_value or 0)} logged conversations"
    if fdef.name == "priority":
        return f"Priority {int(raw_value or 0)} of 3"
    if fdef.name == "n_tags":
        return f"{int(raw_value or 0)} tags"
    return fdef.label_on


def _raw_display(name: str, values: dict[str, Any]) -> Any:
    return {
        "log_revenue": values.get("expected_revenue") or 0,
        "log_messages": values.get("messages") or 0,
        "priority": int(values.get("priority") or 0),
        "n_tags": len(values.get("tags") or []),
    }.get(name)


def predict_lead(artefact: dict[str, Any], values: dict[str, Any], top: int = 5) -> dict[str, Any]:
    """Probability of winning, 0-100 score and the top contributing factors
    (``{feature, label, value, direction, weight}``; weight = contribution to the log-odds)."""
    names = artefact["features"]
    x = _vector(values, names)
    z = (x - np.asarray(artefact["mean"])) / np.asarray(artefact["scale"])
    contrib = np.asarray(artefact["coef"]) * z
    logit = float(artefact["intercept"] + contrib.sum())
    prob = 1.0 / (1.0 + math.exp(-logit))
    defs = {f.name: f for f in feature_definitions(artefact.get("vocab"))}
    order = sorted(range(len(names)), key=lambda i: (-abs(contrib[i]), names[i]))
    factors = []
    for i in order[:top]:
        if abs(contrib[i]) < 1e-6:
            continue
        name = names[i]
        factors.append(
            {
                "feature": name,
                "label": _label(defs[name], float(x[i]), _raw_display(name, values)),
                "value": round(float(x[i]), 4),
                "direction": "positive" if contrib[i] > 0 else "negative",
                "weight": round(float(contrib[i]), 4),
            }
        )
    return {"probability": round(prob, 4), "score": round(prob * 100), "logit": logit, "factors": factors}


def factor_validity(artefact: dict[str, Any], values: dict[str, Any], prediction: dict[str, Any]) -> float:
    """Share of the explanation's factors that hold up: the feature is a model input, its value is the
    record's actual value, and neutralising it (setting it to the training mean) moves the probability
    in the opposite direction of the stated one by at least the stated weight's sign."""
    names = artefact["features"]
    factors = prediction["factors"]
    if not factors:
        return 1.0
    x = _vector(values, names)
    mean = np.asarray(artefact["mean"])
    scale = np.asarray(artefact["scale"])
    coef = np.asarray(artefact["coef"])
    base = float(artefact["intercept"] + (coef * (x - mean) / scale).sum())
    ok = 0
    for f in factors:
        if f["feature"] not in names:
            continue
        i = names.index(f["feature"])
        if abs(float(x[i]) - f["value"]) > 1e-3:
            continue
        ablated = x.copy()
        ablated[i] = mean[i]
        logit = float(artefact["intercept"] + (coef * (ablated - mean) / scale).sum())
        moved = base - logit
        if (moved > 0) == (f["direction"] == "positive") and abs(moved - f["weight"]) < 1e-3:
            ok += 1
    return ok / len(factors)


# ----------------------------------------------------------------------------- deal risk

NEGATIVE_PHRASES = (
    "too expensive",
    "cancel",
    "competitor",
    "postpone",
    "put on hold",
    "on hold",
    "not interested",
    "budget cut",
    "budget freeze",
    "unhappy",
    "disappointed",
    "reconsider",
    "pushing back",
    "delay the decision",
)
RISK_FLAG = 0.5  # a deal is flagged (notification) at or above this risk score
RISK_HIGH = 0.7
STALE_DAYS = 14
STAGE_AGE_DAYS = 45


def negative_phrases(texts: Iterable[str]) -> list[str]:
    found: list[str] = []
    for t in texts:
        low = (t or "").lower()
        found += [p for p in NEGATIVE_PHRASES if p in low and p not in found]
    return found


def deal_risk(signals: dict[str, Any]) -> dict[str, Any]:
    """Risk score (0-1), level and reasons from deal signals:

    ``days_since_activity``, ``stage_age_days``, ``has_next_activity``, ``next_activity_overdue_days``,
    ``days_past_close`` (negative = closing date still ahead), ``negative_phrases`` (list).
    """
    reasons: list[dict[str, Any]] = []

    def add(code: str, weight: float, label: str) -> None:
        reasons.append({"code": code, "weight": round(weight, 3), "label": label})

    idle = signals.get("days_since_activity")
    if idle is None:
        add("no_activity", 0.25, "No conversation logged yet")
    elif idle > STALE_DAYS:
        add("stale_activity", min(0.45, 0.15 + 0.01 * (idle - STALE_DAYS)), f"No activity for {idle} days")
    age = signals.get("stage_age_days") or 0
    if age > STAGE_AGE_DAYS:
        add("stage_age", min(0.3, 0.1 + 0.005 * (age - STAGE_AGE_DAYS)), f"{age} days in the current stage")
    overdue = signals.get("next_activity_overdue_days") or 0
    if overdue > 0:
        add("overdue_activity", min(0.3, 0.15 + 0.01 * overdue), f"Next activity overdue by {overdue} days")
    elif not signals.get("has_next_activity"):
        add("no_next_step", 0.15, "No next activity planned")
    past = signals.get("days_past_close")
    if past is not None and past > 0:
        add("close_date_passed", min(0.35, 0.2 + 0.005 * past), f"Expected closing date passed {past} days ago")
    phrases = list(signals.get("negative_phrases") or [])
    if phrases:
        add("negative_sentiment", 0.35, "Recent messages mention: " + ", ".join(f"“{p}”" for p in phrases[:3]))
    score = min(1.0, sum(r["weight"] for r in reasons))
    level = "high" if score >= RISK_HIGH else "medium" if score >= RISK_FLAG else "low" if reasons else "none"
    reasons.sort(key=lambda r: -r["weight"])
    return {"score": round(score, 3), "level": level, "flagged": score >= RISK_FLAG, "reasons": reasons}


# ----------------------------------------------------------------------------- forecast

HALF_LIFE_MONTHS = 3.0
SIMULATIONS = 4000
PRIOR_STRENGTH = 4.0


def _decayed(rows: Iterable[dict[str, Any]], keys: tuple[str, str]) -> tuple[float, float]:
    a = b = 0.0
    for r in rows:
        w = 0.5 ** (float(r.get("age_months", 0)) / HALF_LIFE_MONTHS)
        a += w * float(r.get(keys[0], 0))
        b += w * float(r.get(keys[1], 0))
    return a, b


def forecast(
    pipeline: list[dict[str, Any]],
    stage_history: dict[str, dict[str, Any]],
    *,
    slip_history: list[dict[str, Any]] | None = None,
    run_rate_history: list[float] | None = None,
    committed: list[float] | None = None,
    periods: int = 3,
    simulations: int = SIMULATIONS,
    seed: int = 0,
) -> list[dict[str, Any]]:
    """P10/P50/P90 of won revenue per period.

    ``pipeline``: open deals ``{id, name, amount, stage, period}`` (period 0 = current).
    ``stage_history``: per stage ``{prior: default probability 0-1, months: [{age_months, won, lost}]}``.
    ``slip_history``: ``[{age_months, slipped, on_time}]`` for won deals (closed after their expected month).
    ``run_rate_history``: monthly revenue of deals created and won within the same month.
    ``committed``: revenue already won in each period.
    """
    rng = np.random.default_rng(seed)
    committed = list(committed or []) + [0.0] * periods
    stages = sorted({d["stage"] for d in pipeline} | set(stage_history))
    posterior: dict[str, tuple[float, float]] = {}
    for s in stages:
        hist = stage_history.get(s) or {}
        prior = min(0.95, max(0.05, float(hist.get("prior", 0.3))))
        won, lost = _decayed(hist.get("months") or [], ("won", "lost"))
        posterior[s] = (won + PRIOR_STRENGTH * prior, lost + PRIOR_STRENGTH * (1 - prior))
    slipped, on_time = _decayed(slip_history or [], ("slipped", "on_time"))
    deals = [d for d in pipeline if 0 <= int(d.get("period", 0)) < periods and float(d.get("amount") or 0) > 0]
    amounts = np.array([float(d["amount"]) for d in deals])
    period_of = np.array([int(d["period"]) for d in deals], dtype=int)
    stage_idx = {s: i for i, s in enumerate(stages)}
    deal_stage = np.array([stage_idx[d["stage"]] for d in deals], dtype=int)
    # parameter draws per simulation (posterior uncertainty), then outcomes per deal
    theta = np.column_stack([rng.beta(*posterior[s], size=simulations) for s in stages]) if stages else None
    slip = rng.beta(slipped + 1.0, on_time + 4.0, size=simulations)
    totals = np.zeros((simulations, periods))
    if deals and theta is not None:
        p = theta[:, deal_stage]
        wins = rng.random((simulations, len(deals))) < p
        slips = rng.random((simulations, len(deals))) < slip[:, None]
        landing = period_of[None, :] + slips.astype(int)
        for k in range(periods):
            totals[:, k] += ((landing == k) & wins).astype(float) @ amounts
    rr = np.asarray([v for v in (run_rate_history or []) if v is not None], dtype=float)
    if len(rr):
        totals += rr[rng.integers(0, len(rr), size=(simulations, periods))]
    out = []
    for k in range(periods):
        sims = totals[:, k] + committed[k]
        in_period = [i for i, d in enumerate(deals) if period_of[i] == k]
        mean_p = {s: posterior[s][0] / sum(posterior[s]) for s in stages}
        weighted = sum(amounts[i] * mean_p[deals[i]["stage"]] for i in in_period)
        spread = sorted(
            in_period,
            key=lambda i: -amounts[i] * math.sqrt(mean_p[deals[i]["stage"]] * (1 - mean_p[deals[i]["stage"]])),
        )
        out.append(
            {
                "period": k,
                "p10": round(float(np.percentile(sims, 10)), 2),
                "p50": round(float(np.percentile(sims, 50)), 2),
                "p90": round(float(np.percentile(sims, 90)), 2),
                "mean": round(float(sims.mean()), 2),
                "components": {
                    "committed": round(committed[k], 2),
                    "open_deals": len(in_period),
                    "open_amount": round(float(sum(amounts[i] for i in in_period)), 2),
                    "weighted_amount": round(float(weighted), 2),
                    "run_rate": round(float(rr.mean()), 2) if len(rr) else 0.0,
                    "slip_rate": round(float((slipped + 1.0) / (slipped + on_time + 5.0)), 4),
                },
                "drivers": [
                    {
                        "id": deals[i].get("id"),
                        "name": deals[i].get("name", ""),
                        "amount": round(float(amounts[i]), 2),
                        "win_rate": round(mean_p[deals[i]["stage"]], 3),
                    }
                    for i in spread[:3]
                ],
                "win_rates": {s: round(mean_p[s], 3) for s in stages},
            }
        )
    return out


def explain_period(row: dict[str, Any], label: str, currency: str = "") -> str:
    c = row["components"]
    cur = f" {currency}" if currency else ""
    parts = [
        f"{label}: most likely {row['p50']:,.0f}{cur} (80% range {row['p10']:,.0f} to {row['p90']:,.0f}{cur}).",
        f"{c['open_deals']} open deals worth {c['open_amount']:,.0f}{cur} are expected to close, "
        f"{c['weighted_amount']:,.0f}{cur} at the win rates of past deals in the same stages",
    ]
    if c["committed"]:
        parts[-1] += f"; {c['committed']:,.0f}{cur} is already won"
    parts[-1] += "."
    if c["run_rate"]:
        parts.append(f"Deals created and won within a month add about {c['run_rate']:,.0f}{cur}.")
    if c["slip_rate"] >= 0.1:
        parts.append(f"About {c['slip_rate']:.0%} of won deals close a month later than expected.")
    if row["drivers"]:
        names = ", ".join(d["name"] or f"#{d['id']}" for d in row["drivers"])
        parts.append(f"The range depends most on: {names}.")
    return " ".join(parts)


# ----------------------------------------------------------------------------- back-test metrics


def precision(flags: Sequence[bool], labels: Sequence[bool]) -> float:
    tp = sum(1 for f, y in zip(flags, labels, strict=True) if f and y)
    flagged = sum(1 for f in flags if f)
    return tp / flagged if flagged else 0.0


def recall(flags: Sequence[bool], labels: Sequence[bool]) -> float:
    tp = sum(1 for f, y in zip(flags, labels, strict=True) if f and y)
    positives = sum(1 for y in labels if y)
    return tp / positives if positives else 0.0


def mape(predicted: Sequence[float], actual: Sequence[float]) -> float:
    pairs = [(p, a) for p, a in zip(predicted, actual, strict=True) if a]
    return sum(abs(p - a) / abs(a) for p, a in pairs) / len(pairs) if pairs else float("nan")
