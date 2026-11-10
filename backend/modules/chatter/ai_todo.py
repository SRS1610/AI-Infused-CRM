"""AI-ranked daily to-do (``chatter.daily_todo``, BUILD_PLAN §2.6).

The ranking is **rules, not a model** (``deterministic: true`` in the manifest): every open activity of
the user gets a score from its deadline, the value of the record it is linked to (weighted by that
record's probability when it has one), the record's priority and the kind of activity, and each score
ships the reasons that produced it. Linked records are read **as the user**: a record the user cannot
read contributes no value, no priority and no field value to the list. Nothing is written except the
audit run (``ai_core.run``) and, for the morning job, an in-app notification with a link.

Interactive on demand (``GET /api/v1/chatter/ai/todo``) and every morning for each user with open
activities (scheduled job ``chatter.ai_daily_todo`` → notification).
"""

from __future__ import annotations

import datetime as dt
import math
import time
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from tessera.ai.errors import AIError, FeatureDisabled
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import AccessError, MissingError
from tessera.i18n import _
from tessera.jobs import job
from tessera.orm import fields as F

FEATURE = "chatter.daily_todo"
RULES_VERSION = "rules-v1"
VALUE_FIELDS = ("expected_revenue", "amount_total", "amount_untaxed", "amount")
TYPE_POINTS = {"call": 6.0, "meeting": 4.0, "email": 3.0, "todo": 0.0, "upload": 0.0}
MEETING_TODAY = 8.0
PRIORITY_POINTS = 3.0  # per star of the linked record's priority
MAX_ITEMS = 50
TODO_URL = "/a/chatter.action_my_activities?ranked=1"


def _readable(rec: Any, name: str) -> bool:
    return name in rec._fields and rec._check_field_access(rec._fields[name], "read")


def linked_facts(env: Any, model: str, res_id: int) -> dict[str, Any]:
    """Value, probability and priority of the linked record, read with the user's rights."""
    if model not in env.registry.models:
        return {"visible": False}
    rec = env[model].browse([res_id])
    try:
        if not rec.exists():
            return {"visible": False}
        rec.check_access("read")
    except (AccessError, MissingError):
        return {"visible": False}
    facts: dict[str, Any] = {"visible": True}
    try:
        for name in VALUE_FIELDS:
            f = rec._fields.get(name)
            if isinstance(f, F.Monetary | F.Float | F.Integer) and _readable(rec, name):
                facts["value"] = float(rec[name] or 0.0)
                currency = getattr(f, "currency_field", None) or "currency_id"
                if _readable(rec, currency) and rec[currency]:
                    facts["currency"] = rec[currency].elevated().name
                break
        if isinstance(rec._fields.get("probability"), F.Float | F.Integer) and _readable(rec, "probability"):
            facts["probability"] = float(rec["probability"] or 0.0)
        if isinstance(rec._fields.get("priority"), F.Selection) and _readable(rec, "priority"):
            raw = str(rec["priority"] or "0")
            facts["priority"] = int(raw) if raw.isdigit() else 0
    except AccessError:  # a field group the user is not in
        return {"visible": True}
    return facts


def _due_reason(days: int) -> tuple[float, str]:
    if days < 0:
        n = -days
        return 50.0 + 2.0 * min(n, 10), (_("Overdue by {count} days", count=n) if n > 1 else _("Overdue by 1 day"))
    if days == 0:
        return 45.0, _("Due today")
    if days == 1:
        return 30.0, _("Due tomorrow")
    if days <= 7:
        return 30.0 - 3.0 * (days - 1), _("Due in {count} days", count=days)
    return max(0.0, 10.0 - (days - 7)), _("Due in {count} days", count=days)


def score_activity(activity: Any, facts: dict[str, Any], today: dt.date) -> tuple[float, list[str]]:
    days = (activity.date_deadline - today).days
    score, due = _due_reason(days)
    reasons = [due]
    category = activity.activity_type_id.category or "todo"
    if category == "meeting" and days == 0:
        score += MEETING_TODAY
        reasons.append(_("Meeting today"))
    else:
        score += TYPE_POINTS.get(category, 0.0)
        if category in ("call", "meeting"):
            reasons.append(_("{type} with other people", type=activity.activity_type_id.name))
    if not facts.get("visible"):
        if activity.model and activity.res_id:
            reasons.append(_("Linked record not visible to you"))
        return score, reasons
    value = facts.get("value") or 0.0
    if value > 0:
        prob = facts.get("probability")
        weighted = value * (prob / 100.0 if prob is not None else 1.0)
        score += 8.0 * math.log10(1.0 + weighted / 1000.0)
        money = f"{value:,.0f}" + (f" {facts['currency']}" if facts.get("currency") else "")
        reasons.append(
            f"{activity.res_name or 'Linked record'} is worth {money}"
            + (f" at {prob:.0f}% probability" if prob is not None else "")
        )
    stars = int(facts.get("priority") or 0)
    if stars:
        score += PRIORITY_POINTS * stars
        reasons.append(_("Priority {stars}", stars="★" * stars))
    return score, reasons


def rank(env: Any, today: dt.date | None = None, limit: int = MAX_ITEMS) -> list[dict[str, Any]]:
    """Open activities of the current user, ranked, with reasons (record rules apply throughout)."""
    today = today or env.today()
    acts = env["chatter.activity"].search([("user_id", "=", env.uid), ("done", "=", False)], order="date_deadline, id")
    cache: dict[tuple[str, int], dict[str, Any]] = {}
    items = []
    for a in acts:
        key = (a.model, a.res_id)
        if key not in cache:
            cache[key] = linked_facts(env, a.model, a.res_id)
        score, reasons = score_activity(a, cache[key], today)
        items.append(
            {
                "activity_id": a.ids[0],
                "summary": a.summary or a.activity_type_id.name,
                "type": a.activity_type_id.name,
                "category": a.activity_type_id.category,
                "model": a.model,
                "res_id": a.res_id,
                "res_name": a.res_name,
                "date_deadline": a.date_deadline.isoformat(),
                "score": round(score, 2),
                "reasons": reasons,
            }
        )
    items.sort(key=lambda i: (-i["score"], i["date_deadline"], i["activity_id"]))
    for n, item in enumerate(items, 1):
        item["rank"] = n
    return items[:limit]


def as_text(items: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"{i['rank']}. {i['type']}: {i['summary']}"
        + (f" — {i['res_name']}" if i.get("res_name") else "")
        + f" ({'; '.join(i['reasons'])})"
        for i in items
    )


def _feature(env: Any) -> Any:
    from tessera.ai.runtime import check_enabled, get_feature

    if "ai_core.feature" not in env.registry.models:
        raise FeatureDisabled(_("AI features are not installed"))
    feature = get_feature(env, FEATURE)
    check_enabled(env, feature)
    return feature


def _record_run(env: Any, feature: Any, items: list[dict[str, Any]], text: str, started: float) -> Any:
    """Audit row (and the run id the UI's feedback buttons refer to). No provider was called."""
    prompt = env["ai_core.prompt"].elevated().search([("name", "=", feature.prompt or feature.name)], limit=1)
    company = env.company.ids[0] if env.company else None
    version = prompt.version_for(company, env.uid) if prompt else None
    return (
        env["ai_core.run"]
        .elevated("ai run")
        .create(
            {
                "feature_id": feature.ids[0],
                "feature": FEATURE,
                "user_id": env.uid,
                "company_id": company,
                "prompt_version": version.ref if version else None,
                "provider": "rules",
                "model": RULES_VERSION,
                "status": "ok",
                "input_ref": {"activity_ids": [i["activity_id"] for i in items]},
                "output_text": text,
                "output_data": {"items": items},
                "latency_ms": int((time.monotonic() - started) * 1000),
                "cost": 0.0,
                "steps": 0,
                "decision": "none",
            }
        )
    )


def daily_todo(env: Any, today: dt.date | None = None) -> dict[str, Any]:
    started = time.monotonic()
    feature = _feature(env)
    items = rank(env, today)
    text = as_text(items)
    run = _record_run(env, feature, items, text, started)
    return {"run_id": run.ids[0], "items": items, "text": text, "deterministic": True, "ai_generated": True}


# ----------------------------------------------------------------------------- API

router = register_router(APIRouter(prefix="/chatter/ai", tags=["chatter", "ai"]))


@router.get("/todo", response_model=None)
def todo(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any] | JSONResponse:
    try:
        return daily_todo(ctx.env)
    except AIError as exc:
        return JSONResponse(status_code=exc.http_status, content={"error": type(exc).__name__, "message": exc.message})


# ----------------------------------------------------------------------------- morning job


@job("chatter.ai_daily_todo", queue="ai", max_attempts=1, as_system=True, timeout_seconds=1800)
def morning_job(env: Any) -> dict[str, int]:
    """For every internal user with open activities due within a day: rank them (as that user) and
    post an in-app notification linking to the ranked list."""
    if "ai_core.feature" not in env.registry.models:
        return {"users": 0}
    horizon = (env.today() + dt.timedelta(days=1)).isoformat()
    rows = env["chatter.activity"].search([("done", "=", False), ("date_deadline", "<=", horizon)])
    user_ids = sorted({a.user_id.ids[0] for a in rows if a.user_id})
    notified = 0
    for user in env["core.user"].browse(user_ids):
        if user.share or not user.active:
            continue
        user_env = env.with_user(user.ids[0]).with_company(user.company_id.ids[0]) if user.company_id else None
        if user_env is None:
            continue
        try:
            result = daily_todo(user_env)
        except (AIError, AccessError):
            continue  # feature off for this company, or not an internal user
        items = result["items"]
        if not items:
            continue
        top = items[0]
        env["core.notification"].create(
            {
                "user_id": user.ids[0],
                "title": _("Your AI-ranked to-do for today: {count} activities", count=len(items))
                if len(items) > 1
                else _("Your AI-ranked to-do for today: 1 activity"),
                "body": _(
                    "Start with {type}: {summary} ({reasons})",
                    type=top["type"].lower(),
                    summary=top["summary"],
                    reasons="; ".join(top["reasons"][:2]),
                ),
                "kind": "activity",
                "url": TODO_URL,
            }
        )
        notified += 1
    return {"users": notified}


# ----------------------------------------------------------------------------- evaluation


def eval_entry(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    """Eval harness entry (``eval.entry``): ``inputs.today`` pins the reference date of a case."""
    today = dt.date.fromisoformat(inputs["today"]) if inputs.get("today") else None
    result = daily_todo(env, today)
    return {"text": result["text"], "data": {"items": result["items"]}, "proposals": 0, "status": "ok"}


def spearman(predicted: list[int], labelled: list[int]) -> float:
    """Spearman rank correlation of two orderings of the same items (no ties)."""
    common = [x for x in labelled if x in predicted]
    n = len(common)
    if n < 2:
        return 1.0 if n == 1 else 0.0
    pred_rank = {x: i for i, x in enumerate([p for p in predicted if p in common])}
    d2 = sum((pred_rank[x] - i) ** 2 for i, x in enumerate(common))
    return 1.0 - 6.0 * d2 / (n * (n * n - 1))


def spearman_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    """Aggregate eval scorer: mean Spearman correlation with the labelled order, and top-1 agreement."""
    rhos, top1 = [], []
    for r in results:
        order = r["_case"].get("expected", {}).get("order")
        if not order:
            continue
        items = (r.get("data") or {}).get("items") or [] if r.get("status") == "ok" else []
        predicted = [i["activity_id"] for i in items]
        rhos.append(spearman(predicted, order))
        top1.append(1.0 if predicted[:1] == order[:1] else 0.0)
    if not rhos:
        return {}
    return {"rank_correlation": sum(rhos) / len(rhos), "top1_agreement": sum(top1) / len(top1)}
