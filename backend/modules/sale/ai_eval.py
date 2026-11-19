"""Evaluation entry points and aggregate scorers of the sales AI features (``eval.entry`` / ``eval.scorer`` in
``ai/features.yaml``). Entries run the production code paths on the case's records as the case's user;
scorers compute the metrics over all cases — nothing is hard-coded.

* ``sale.upsell``: a history back-test (train on the synthetic orders before the cut-off, hide one product of a
  later order, check it is in the top 3) plus cases on database orders through :func:`suggest_products`.
* ``sale.discount_suggest``: every suggestion is checked against its band as stored in the database.
* ``sale.pricing_breach``: precision and recall of the flagged (line, rule) pairs on a labelled set.
* ``sale.quote_from_text``: line F1 on (product, quantity) and fabricated products (a proposed product that
  does not exist, cannot be sold or cannot be read by the user).
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from functools import lru_cache
from typing import Any

from modules.sale import ai_common as AC
from modules.sale import ai_discount, ai_pricing, ai_quote_text, ai_synth, ai_upsell


def _result(data: dict[str, Any], proposals: int = 0) -> dict[str, Any]:
    return {"data": data, "text": json.dumps(data, sort_keys=True, default=str), "proposals": proposals, "flags": []}


def _twin_invariance(results: list[dict[str, Any]], key: str) -> float | None:
    by_id = {r["case"]: r for r in results}
    checks = []
    for r in results:
        twin = (r["_case"].get("expected") or {}).get("same_as")
        if twin and twin in by_id and r.get("data") is not None and by_id[twin].get("data") is not None:
            checks.append(1.0 if r["data"].get(key) == by_id[twin]["data"].get(key) else 0.0)
    return sum(checks) / len(checks) if checks else None


def _ok(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]


# ----------------------------------------------------------------------------- upsell


@lru_cache(maxsize=4)
def backtest_model(cutoff: str) -> ai_upsell.CoPurchase:
    return ai_upsell.CoPurchase.fit(ai_synth.training_baskets(dt.date.fromisoformat(cutoff)))


def eval_upsell(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    AC.ensure_enabled(env, ai_upsell.FEATURE)
    if "basket" in inputs:  # history back-test
        model = backtest_model(str(inputs["cutoff"]))
        basket = list(inputs["basket"])
        top = [str(r.key) for r in model.rank(basket, limit=3)]
        popular = sorted(model.count, key=lambda k: (-model.count[k], str(k)))
        baseline = [str(p) for p in popular if p not in basket][:3]
        return _result({"mode": "backtest", "top3": top, "baseline_top3": baseline, "ranking": top})
    found = ai_upsell.suggest_products(record, limit=3)
    rows = found["suggestions"]
    # the ranking by name lets twin cases (same data, planted instructions) be compared across setups
    return _result(
        {"mode": "db", "top3": [s["product_id"] for s in rows], "ranking": [s["display_name"] for s in rows]}
    )


def score_upsell(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in _ok(results) if "hidden" in (r["_case"].get("expected") or {})]
    hits = [r for r in ok if r["_case"]["expected"]["hidden"] in r["data"]["top3"]]
    first = [r for r in ok if r["data"]["top3"][:1] == [r["_case"]["expected"]["hidden"]]]
    back = [r for r in ok if r["data"]["mode"] == "backtest"]
    db = [r for r in ok if r["data"]["mode"] == "db"]
    out = {
        "top3_precision": len(hits) / len(ok) if ok else 0.0,
        "top1_precision": len(first) / len(ok) if ok else 0.0,
        "backtest_cases": float(len(back)),
    }
    if back:
        out["top3_backtest"] = sum(1 for r in back if r in hits) / len(back)
        out["top3_popularity_baseline"] = sum(
            1 for r in back if r["_case"]["expected"]["hidden"] in r["data"]["baseline_top3"]
        ) / len(back)
    if db:
        out["top3_database"] = sum(1 for r in db if r in hits) / len(db)
    inv = _twin_invariance(results, "ranking")
    if inv is not None:
        out["injection_invariance"] = inv
    return out


# ----------------------------------------------------------------------------- discounts


def eval_discount(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    before = {line.ids[0]: float(line.discount or 0.0) for line in record.elevated().line_ids}
    out = ai_discount.suggest(record)
    env.cache.invalidate()
    after = {line.ids[0]: float(line.discount or 0.0) for line in record.elevated().line_ids}
    bands = env["sale.discount_band"].elevated().with_context(active_test=False)
    rows = []
    proposals = 0
    for r in out["lines"]:
        row: dict[str, Any] = {"line_id": r["line_id"], "status": r["status"], "band_id": None}
        if r["status"] != "no_band":
            band = bands.browse([r["band"]["id"]])
            low, high = float(band.min_discount or 0.0), float(band.max_discount or 0.0)
            row.update(band_id=band.ids[0], suggested=r["suggested_discount"], method=r["method"])
            row["within"] = ai_discount.within_band(r["suggested_discount"], low, high)
            if r.get("proposal"):
                proposals += 1
                proposal = env["ai_core.proposal"].elevated().browse([r["proposal"]["id"]])
                row["proposal_within"] = ai_discount.within_band(float(proposal.payload["discount"]), low, high)
        rows.append(row)
    data = {"lines": rows, "unchanged_before_accept": before == after, "suggested": [r.get("suggested") for r in rows]}
    return _result(data, proposals)


def score_discount(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = _ok(results)
    suggestions = [row for r in ok for row in r["data"]["lines"] if "within" in row]
    proposed = [row for row in suggestions if "proposal_within" in row]
    labelled = []
    for r in ok:
        pairs = (r["_case"].get("expected") or {}).get("bands")
        if pairs is None:
            continue
        wanted = {int(line): band for line, band in pairs}
        labelled += [(row, wanted.get(row["line_id"])) for row in r["data"]["lines"]]
    out = {
        "within_band": sum(1 for s in suggestions if s["within"]) / len(suggestions) if suggestions else 0.0,
        "proposals_within_band": sum(1 for s in proposed if s["proposal_within"]) / len(proposed) if proposed else 1.0,
        "suggestions": float(len(suggestions)),
        "band_accuracy": sum(1 for row, exp in labelled if row["band_id"] == exp) / len(labelled) if labelled else 0.0,
        "zero_mutations": sum(1 for r in ok if r["data"]["unchanged_before_accept"]) / len(ok) if ok else 0.0,
        "history_method": sum(1 for s in suggestions if s.get("method") == "history") / len(suggestions)
        if suggestions
        else 0.0,
    }
    inv = _twin_invariance(results, "suggested")
    if inv is not None:
        out["injection_invariance"] = inv
    return out


# ----------------------------------------------------------------------------- pricing flags


def eval_pricing(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    found = ai_pricing.check(record)
    flags = sorted([f["line_id"], f["code"]] for f in found)
    order = {line.ids[0]: i for i, line in enumerate(record.elevated().line_ids)}
    codes = sorted([order.get(f["line_id"], -1), f["code"]] for f in found)  # comparable across twin setups
    return _result({"flags": flags, "codes": codes, "severities": sorted({f["severity"] for f in found})})


def score_pricing(results: list[dict[str, Any]]) -> dict[str, float]:
    tp = fp = fn = 0
    q_tp = q_fp = 0
    for r in _ok(results):
        expected = {(int(a), str(b)) for a, b in (r["_case"].get("expected") or {}).get("flags", [])}
        got = {(int(a), str(b)) for a, b in r["data"]["flags"]}
        tp += len(got & expected)
        fp += len(got - expected)
        fn += len(expected - got)
        if got:
            q_tp += 1 if expected else 0
            q_fp += 0 if expected else 1
    out = {
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "quotation_precision": q_tp / (q_tp + q_fp) if q_tp + q_fp else 1.0,
        "flags_tp": float(tp),
        "flags_fp": float(fp),
        "flags_fn": float(fn),
    }
    inv = _twin_invariance(results, "codes")
    if inv is not None:
        out["injection_invariance"] = inv
    return out


# ----------------------------------------------------------------------------- lines from text


def eval_quote(env: Any, inputs: dict[str, Any], record: Any) -> Any:
    """The API path, then the fabrication audit of every proposed product (as the user)."""
    from tessera.ai.runtime import HANDLERS, run_feature

    feature = ai_quote_text.FEATURE
    result = run_feature(env, feature, inputs, record=record, **HANDLERS[feature](env, record, inputs))
    proposed: list[list[Any]] = []
    for p in result.proposals:
        for cmd in p.payload.get("line_ids") or []:
            vals = cmd[1] if isinstance(cmd, list | tuple) and len(cmd) > 1 else {}
            proposed.append([int(vals.get("product_id") or 0), float(vals.get("quantity") or 0.0)])
    products = env["product.product"]
    readable = set(products.search([("id", "in", [pid for pid, _ in proposed]), ("is_sellable", "=", True)]).ids)
    fabricated = [pid for pid, _ in proposed if pid not in readable]
    if isinstance(result.data, dict):
        result.data["proposed"] = proposed
        result.data["fabricated"] = fabricated
    return result


def score_quote(results: list[dict[str, Any]]) -> dict[str, float]:
    tp = fp = fn = 0
    flag_hits = flag_total = 0
    fabricated = 0
    for r in _ok(results):
        exp = r["_case"].get("expected") or {}
        if "lines" not in exp:
            continue
        expected = Counter((int(p), float(q)) for p, q in exp["lines"])
        got = Counter((int(p), float(q)) for p, q in r["data"].get("proposed") or [])
        common = sum((expected & got).values())
        tp += common
        fp += sum(got.values()) - common
        fn += sum(expected.values()) - common
        fabricated += len(r["data"].get("fabricated") or [])
        flag_total += int(exp.get("flagged", 0))
        flag_hits += min(int(exp.get("flagged", 0)), len(r["data"].get("flagged") or []))
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {
        "line_f1": 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall),
        "line_precision": precision,
        "line_recall": recall,
        "fabricated_products": float(fabricated),
        "flagged_recall": flag_hits / flag_total if flag_total else 1.0,
    }
