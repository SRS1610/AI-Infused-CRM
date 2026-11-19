"""Evaluation entries and scorers of the asset AI features (``ai/features.yaml``).

* ``classify_entry``: the case's ``setup`` creates the company's asset models, some earlier assets (history)
  and a draft vendor bill; the entry classifies its first product line as the case's user. Accuracy compares
  the suggested model with ``expected.model``; the asset count must not change before acceptance.
* ``check_entry``: the case's ``setup`` creates an asset model, the asset (and peers); the entry confirms
  them, posts the entries due by ``inputs.today`` unless the case keeps them as drafts, applies the case's
  manipulation (``inputs.tamper``: a depreciation entry's amount changed behind the board's back) and runs the
  checks. Precision and recall are over assets (flagged vs labelled anomalous), ``code_accuracy`` over the
  anomalous ones (the seeded anomaly's code is among the findings).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account_asset import ai_features as A


def _asset_count(env: Any) -> int:
    env.flush()
    return int(env.cr.scalar("SELECT count(*) FROM account_asset") or 0)


def classify_entry(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    bill = record
    line = bill.elevated().invoice_line_ids.filtered(lambda r: r.display_type == "product")[:1]
    before = _asset_count(env)
    out = A.suggest_model(env, env["account.move_line"].browse(line.ids))
    after = _asset_count(env)
    best = out["suggestion"]
    data = {
        "model_id": best["model_id"] if best else None,
        "ranked": [(r["model_id"], r["score"]) for r in out["ranked"]],
        "used_model": out["used_model"],
        "mutations": after - before,
        "text": out["text"],
    }
    return {"text": "", "data": data, "proposals": 1 if out["proposal"] else 0, "status": "ok", "flags": out["flags"]}


def classify_score(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    hits = [r["data"]["model_id"] == r["_case"]["expected"].get("model") for r in ok]
    return {
        "accuracy": sum(hits) / len(ok),
        "zero_mutations": sum(1 for r in ok if r["data"]["mutations"] == 0) / len(ok),
        "model_calls": float(sum(1 for r in ok if r["data"]["used_model"])),
    }


def check_entry(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    today = dt.date.fromisoformat(inputs["today"])
    sys_env = env.elevated("ai eval scenario")
    asset = sys_env["account.asset"].browse(record.ids)
    for pid in inputs.get("peers") or []:
        peer = sys_env["account.asset"].browse([int(pid)])
        peer.action_validate()
        peer.action_post_due(today.isoformat())
    if inputs.get("validate", True):
        asset.action_validate()
        drop = inputs.get("drop")
        if drop:  # a period whose planned entry was deleted before posting
            drafts = asset.depreciation_move_ids.filtered(lambda m: m.state == "draft").sorted(lambda m: m.date)
            drafts[drop["index"]].unlink()
        if inputs.get("post_due", True):
            asset.action_post_due(today.isoformat())
        duplicate = inputs.get("duplicate")
        if duplicate:  # the entry of a period posted a second time
            posted = asset.depreciation_move_ids.filtered(lambda m: m.state == "posted").sorted(lambda m: m.date)
            first = posted[duplicate["index"]]
            vals = asset._entry_vals(first.date, float(first.asset_depreciation_value or 0), 0.0)
            sys_env["account.move"].create(vals).action_post()
    tamper = inputs.get("tamper")
    if tamper:
        drafts = asset.depreciation_move_ids.filtered(lambda m: m.state == "draft").sorted(lambda m: m.date)
        target = drafts[tamper.get("index", 0)]
        target.with_context(asset_internal=True).write(
            {"asset_depreciation_value": float(target.asset_depreciation_value or 0) * tamper["factor"]}
        )
    out = A.check_asset(env, record, today)
    data = {"flagged": out["flagged"], "codes": sorted({i["code"] for i in out["issues"]})}
    return {"text": "", "data": data, "proposals": 0, "status": "ok", "flags": []}


def check_score(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    tp = sum(1 for r in ok if r["data"]["flagged"] and r["_case"]["expected"]["anomalous"])
    fp = sum(1 for r in ok if r["data"]["flagged"] and not r["_case"]["expected"]["anomalous"])
    fn = sum(1 for r in ok if not r["data"]["flagged"] and r["_case"]["expected"]["anomalous"])
    anomalous = [r for r in ok if r["_case"]["expected"]["anomalous"]]
    coded = sum(1 for r in anomalous if set(r["_case"]["expected"].get("codes") or []) <= set(r["data"]["codes"]))
    return {
        "precision": tp / (tp + fp) if tp + fp else 1.0,
        "recall": tp / (tp + fn) if tp + fn else 1.0,
        "code_accuracy": coded / len(anomalous) if anomalous else 1.0,
        "false_positives": float(fp),
    }
