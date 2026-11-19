"""Evaluation entry points and aggregate scorers of the CRM insight features (``eval.entry`` /
``eval.scorer`` in ``ai/features/insights.yaml``).

The entries run the production code paths on the case's records (feature extraction, scoring,
signals, routing) and return plain results; the scorers compute the back-test metrics over all cases
(AUC, explanation validity, precision, accuracy, MAPE, interval coverage) — nothing is hard-coded.
The lead-scoring model is trained on a synthetic history drawn from the same process as the cases
but with a different seed (see :mod:`modules.crm.ai_insights_synth`)."""

from __future__ import annotations

import datetime as dt
import json
from functools import lru_cache
from typing import Any

from modules.crm import ai_insights as AI
from modules.crm import ai_insights_ml as ML
from modules.crm import ai_insights_synth as S


@lru_cache(maxsize=1)
def backtest_model() -> dict[str, Any]:
    rows, labels = S.lead_history()
    return ML.fit_lead_model(rows, labels, seed=S.TRAINING_SEED)


def _result(data: dict[str, Any], proposals: int = 0) -> dict[str, Any]:
    return {"data": data, "text": json.dumps(data, sort_keys=True, default=str), "proposals": proposals, "flags": []}


def _twin_invariance(results: list[dict[str, Any]], key: str) -> float | None:
    by_id = {r["case"]: r for r in results}
    checks = []
    for r in results:
        twin = r["_case"].get("expected", {}).get("same_as")
        if twin and twin in by_id and r.get("data") and by_id[twin].get("data"):
            checks.append(1.0 if r["data"].get(key) == by_id[twin]["data"].get(key) else 0.0)
    return sum(checks) / len(checks) if checks else None


# ----------------------------------------------------------------------------- lead scoring


def eval_lead_score(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    AI.ensure_enabled(env, AI.SCORE)
    record.check_access("read")
    artefact = backtest_model()
    values = AI.lead_values(record)
    pred = ML.predict_lead(artefact, values)
    return _result(
        {
            "score": pred["score"],
            "probability": pred["probability"],
            "factors": pred["factors"],
            "factor_validity": ML.factor_validity(artefact, values, pred),
            "rule_score": int(record.elevated().score or 0),
        }
    )


def score_lead_score(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r["status"] == "ok" and r.get("data") and "won" in r["_case"].get("expected", {})]
    labels = [int(r["_case"]["expected"]["won"]) for r in ok]
    out = {
        "auc": ML.auc(labels, [r["data"]["probability"] for r in ok]),
        "auc_rules_baseline": ML.auc(labels, [r["data"]["rule_score"] for r in ok]),
        "factor_validity": sum(r["data"]["factor_validity"] for r in ok) / len(ok) if ok else 0.0,
        "top5_factors": sum(1 for r in ok if 0 < len(r["data"]["factors"]) <= 5) / len(ok) if ok else 0.0,
    }
    inv = _twin_invariance(results, "score")
    if inv is not None:
        out["injection_invariance"] = inv
    return out


# ----------------------------------------------------------------------------- deal risk


def eval_deal_risk(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    AI.ensure_enabled(env, AI.RISK)
    record.check_access("read")
    as_of = dt.datetime.fromisoformat(inputs["as_of"]) if inputs.get("as_of") else None
    risk = AI.assess_deal(record, as_of)
    return _result(
        {
            "flagged": risk["flagged"],
            "level": risk["level"],
            "score": risk["score"],
            "reasons": [r["code"] for r in risk["reasons"]],
        }
    )


def score_deal_risk(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r["status"] == "ok" and r.get("data") and "at_risk" in r["_case"].get("expected", {})]
    flags = [bool(r["data"]["flagged"]) for r in ok]
    labels = [bool(r["_case"]["expected"]["at_risk"]) for r in ok]
    out = {
        "precision": ML.precision(flags, labels),
        "recall": ML.recall(flags, labels),
        "flag_rate": sum(flags) / len(flags) if flags else 0.0,
        "reasons_given": sum(1 for r in ok if not r["data"]["flagged"] or r["data"]["reasons"]) / len(ok)
        if ok
        else 0.0,
    }
    inv = _twin_invariance(results, "score")
    if inv is not None:
        out["injection_invariance"] = inv
    return out


# ----------------------------------------------------------------------------- lead routing


def eval_lead_routing(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    before = (record.elevated().user_id.ids[:1], record.elevated().team_id.ids[:1])
    out = AI.propose_routing(env, record)
    env.cache.invalidate()
    after = (record.elevated().user_id.ids[:1], record.elevated().team_id.ids[:1])
    row = out["rows"][0]
    proposals = sum(1 for r in out["rows"] if r.get("proposal"))
    return _result(
        {
            "user_id": row["user_id"],
            "team_id": row["team_id"],
            "status": row["status"],
            "reasons": row["reasons"],
            "unchanged_before_accept": before == after,
        },
        proposals,
    )


def score_lead_routing(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r["status"] == "ok" and r.get("data")]
    labelled = [r for r in ok if "user_id" in r["_case"].get("expected", {}).get("data", {})]
    hits = [1.0 if r["data"]["user_id"] == r["_case"]["expected"]["data"]["user_id"] else 0.0 for r in labelled]
    return {
        "accuracy": sum(hits) / len(hits) if hits else 0.0,
        "zero_mutations": sum(1 for r in ok if r["data"]["unchanged_before_accept"]) / len(ok) if ok else 0.0,
        "reasons_given": sum(1 for r in ok if r["data"]["reasons"]) / len(ok) if ok else 0.0,
    }


# ----------------------------------------------------------------------------- forecast


def eval_forecast(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    AI.ensure_enabled(env, AI.FORECAST)
    rows = ML.forecast(
        inputs["pipeline"],
        inputs["stage_history"],
        slip_history=inputs.get("slip_history"),
        run_rate_history=inputs.get("run_rate_history"),
        committed=inputs.get("committed"),
        periods=1,
        seed=int(inputs.get("seed", 0)),
    )
    row = rows[0]
    row["explanation"] = ML.explain_period(row, "This month")
    return _result({k: row[k] for k in ("p10", "p50", "p90", "explanation")})


def score_forecast(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r["status"] == "ok" and r.get("data") and "actual" in r["_case"].get("expected", {})]
    actual = [float(r["_case"]["expected"]["actual"]) for r in ok]
    out = {
        "mape": ML.mape([r["data"]["p50"] for r in ok], actual),
        "coverage_p10_p90": sum(1 for r, a in zip(ok, actual, strict=True) if r["data"]["p10"] <= a <= r["data"]["p90"])
        / len(ok)
        if ok
        else 0.0,
    }
    inv = _twin_invariance(results, "p50")
    if inv is not None:
        out["injection_invariance"] = inv
    return out
