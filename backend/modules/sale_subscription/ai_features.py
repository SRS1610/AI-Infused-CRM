"""AI features of subscriptions (sub-phase 2.6, BUILD_PLAN §3).

``sale_subscription.churn_predict`` — churn prediction (in-process, no provider call)
    A logistic regression on :func:`modules.sale_subscription.metrics.churn_features`
    (:mod:`modules.sale_subscription.ai_churn`): the company's own model once it has enough ended and surviving
    subscriptions, documented prior weights before that. Each score carries its top contributing factors. The daily
    job (and *Score now*) stores the score on the subscription; the form shows it with its factors and the list has
    an *At risk* filter. Nothing else is written. An optional narration (mid tier) restates the factors in words;
    it never produces the number.

``sale_subscription.renewal_draft`` — personalised renewal outreach (mid tier, outbound → proposal)
    From the subscription's plan, lines, tenure, MRR history, renewal quotation and churn risk, the model writes a
    renewal e-mail whose tone follows the risk. Every number in the draft must be one of the facts (amounts,
    dates, counts): sentences stating another figure are removed (:func:`tessera.ai.facts.ground_text`). The draft
    is a **message proposal** on the subscription; nothing is posted or e-mailed until the user sends it.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import TYPE_CHECKING, Any

from modules.sale.models.quotation import INTERNAL
from modules.sale_subscription import ai_churn as ML
from modules.sale_subscription.billing import TODAY_KEY, today
from modules.sale_subscription.metrics import churn_features
from modules.sale_subscription.models.subscription import RUNNING
from tessera.ai import Source
from tessera.ai.facts import audit_run, ensure_enabled, feature_enabled, ground_text, ungrounded
from tessera.ai.runtime import HANDLERS, RunResult, handler, run_feature
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.jobs import job
from tessera.logging import get_logger

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)
CHURN = "sale_subscription.churn_predict"
RENEWAL = "sale_subscription.renewal_draft"
TONES = {"high": "retention", "medium": "reassuring", "low": "appreciative"}
SURVIVOR_DAYS = 180  # a subscription running this long without churning is a training example of "stays"


# ============================================================================================ churn prediction


def _running(env: Environment, company_id: int) -> Model:
    return (
        env["sale.quotation"]
        .elevated("churn scoring")
        .search(
            [
                ("is_subscription", "=", True),
                ("state", "=", "accepted"),
                ("subscription_state", "in", list(RUNNING)),
                ("upsell_of_id", "=", None),
                ("company_id", "=", company_id),
            ]
        )
    )


def training_set(env: Environment, company_id: int, day: dt.date) -> tuple[list[dict[str, Any]], list[int]]:
    """Churned subscriptions (1) and subscriptions that stayed (0): renewed, or running for at least
    :data:`SURVIVOR_DAYS`. Facts as of their end (churned) or of ``day``."""
    subs = (
        env["sale.quotation"]
        .elevated("churn training")
        .search(
            [
                ("is_subscription", "=", True),
                ("state", "=", "accepted"),
                ("upsell_of_id", "=", None),
                ("company_id", "=", company_id),
                ("subscription_state", "in", ["progress", "paused", "closed", "churned"]),
            ]
        )
    )
    rows, labels = [], []
    for sub in subs:
        facts = churn_features(sub, day)
        if sub.subscription_state == "churned":
            rows.append(facts)
            labels.append(1)
        elif sub.subscription_state == "closed" or facts["tenure_days"] >= SURVIVOR_DAYS:
            rows.append(facts)
            labels.append(0)
    return rows, labels


def active_model(env: Environment, company_id: int) -> Model | None:
    """The company's latest trained model whose artefact is intact, or None (prior weights)."""
    found = (
        env["sale.subscription_churn_model"]
        .elevated()
        .search([("company_id", "=", company_id), ("active", "=", True)], order="trained_at desc, id desc", limit=1)
    )
    if found and found.artefact and ML.artefact_hash(found.artefact) == found.artefact_hash:
        return found
    return None


def train_company_model(env: Environment, company_id: int, day: dt.date) -> Model | None:
    """Train and store the company's model when its history allows it (else keep the previous one / the prior)."""
    rows, labels = training_set(env, company_id, day)
    try:
        artefact = ML.fit_churn_model(rows, labels)
    except ValueError:
        return active_model(env, company_id)
    models = env["sale.subscription_churn_model"].elevated("churn training")
    models.search([("company_id", "=", company_id), ("active", "=", True)]).write({"active": False})
    return models.create(
        {
            "name": f"Churn model {day.isoformat()}",
            "company_id": company_id,
            "algorithm": artefact["algorithm"],
            "artefact": artefact,
            "artefact_hash": artefact["hash"],
            "metrics": artefact["metrics"],
            "trained_at": env.now(),
        }
    )


def artefact_for(env: Environment, company_id: int) -> tuple[dict[str, Any], Model | None]:
    model = active_model(env, company_id)
    return (model.artefact, model) if model is not None else (ML.PRIOR, None)


def predict_subscription(sub: Model, day: dt.date | None = None) -> dict[str, Any]:
    """Churn facts, prediction and model of one subscription (nothing stored)."""
    rec = sub.elevated()
    day = day or today(sub.env)
    facts = churn_features(rec, day)
    artefact, model = artefact_for(sub.env, rec.company_id.ids[0])
    pred = ML.predict(artefact, facts)
    return {"facts": facts, "prediction": pred, "model": model, "artefact": artefact}


def score_subscriptions(subs: Model, day: dt.date | None = None) -> int:
    """Score running subscriptions of companies where the feature is on and store the result on them."""
    env = subs.env
    day = day or today(env)
    now = env.now()
    done = 0
    for sub in subs.elevated("churn scoring"):
        if sub.subscription_state not in RUNNING or not feature_enabled(env, CHURN, sub.company_id.ids[0]):
            continue
        out = predict_subscription(sub, day)
        pred = out["prediction"]
        facts = {k: v for k, v in out["facts"].items() if not k.startswith("_")}
        sub.with_context(**{INTERNAL: True}).write(
            {
                "churn_probability": pred["probability"],
                "churn_score": pred["score"],
                "churn_risk": pred["risk"],
                "churn_factors": pred["factors"],
                "churn_facts": facts,
                "churn_scored_at": now,
                "churn_model_id": out["model"].ids[0] if out["model"] is not None else None,
            }
        )
        done += 1
    return done


_CLEARED = {
    "churn_probability": 0.0,
    "churn_score": 0,
    "churn_risk": None,
    "churn_factors": None,
    "churn_facts": None,
    "churn_scored_at": None,
    "churn_model_id": None,
}


@job("sale_subscription.ai_churn", queue="ai", max_attempts=2, as_system=True, timeout_seconds=3600)
def churn_job(env: Any, company_ids: list[int] | None = None) -> dict[str, Any]:
    """Daily: retrain each company's churn model when it has the history, then score its running subscriptions.
    Companies where the feature is off get their stored scores cleared (the *At risk* filter shows nothing)."""
    started = time.monotonic()
    companies = env["core.company"].elevated().search([("id", "in", company_ids)] if company_ids else [])
    day = today(env)
    out: dict[str, Any] = {}
    for company in companies:
        cid = company.ids[0]
        subs = _running(env, cid)
        if not feature_enabled(env, CHURN, cid):
            stale = subs.filtered(lambda s: s.churn_scored_at)
            if stale:
                stale.with_context(**{INTERNAL: True}).write(_CLEARED)
            out[str(cid)] = {"skipped": True, "cleared": len(stale)}
            continue
        model = train_company_model(env, cid, day)
        scored = score_subscriptions(subs, day)
        result = {"scored": scored, "model": model.ids[0] if model is not None else "prior"}
        audit_run(
            env.with_company(cid), CHURN, inputs={"company_id": cid, "day": day}, output=result, started=started,
            model=(model.algorithm if model is not None else "prior"),
        )  # fmt: skip
        out[str(cid)] = result
    log.info("churn scoring", result=out)
    return out


def _factor_rows(rec: Model) -> list[dict[str, Any]]:
    facts = rec.churn_facts or {}
    currency = facts.get("currency") or ""
    return [
        {**f, "label": ML.label(f["feature"], facts, str(currency))}
        for f in (rec.churn_factors or [])
        if f.get("feature") in ML.NAMES
    ]


def churn_view(env: Environment, sub: Model) -> dict[str, Any]:
    """What the subscription form shows: the stored score with its factors (in the user's language)."""
    ensure_enabled(env, CHURN)
    sub.check_access("read")
    rec = sub.elevated()
    model = rec.churn_model_id
    metrics = (model.metrics or {}) if model else {}
    return {
        "subscription_id": rec.ids[0],
        "is_subscription": bool(rec.is_subscription),
        "running": rec.subscription_state in RUNNING,
        "score": rec.churn_score if rec.churn_scored_at else None,
        "probability": rec.churn_probability if rec.churn_scored_at else None,
        "risk": rec.churn_risk or None,
        "factors": _factor_rows(rec),
        "scored_at": rec.churn_scored_at.isoformat() if rec.churn_scored_at else None,
        "model": {
            "source": "company" if model else "prior",
            "trained_at": model.trained_at.isoformat() if model and model.trained_at else None,
            "rows": metrics.get("rows"),
            "holdout_auc": metrics.get("holdout_auc"),
        },
        "excluded_inputs": str(
            _("The subscription's state, close reason and the customer's identity are never model inputs")
        ),
        "ai_generated": True,
    }


def rescore(env: Environment, sub: Model) -> dict[str, Any]:
    """*Score now*: compute and store this subscription's score as the requesting user, with an audit run."""
    ensure_enabled(env, CHURN)
    sub.check_access("read")
    started = time.monotonic()
    rec = sub.elevated()
    if not rec.is_subscription or rec.subscription_state not in RUNNING:
        raise UserError(_("Only running subscriptions are scored"))
    score_subscriptions(sub)
    view = churn_view(env, sub)
    run = audit_run(
        env, CHURN, record=sub, inputs={"day": today(env)}, started=started, model=view["model"]["source"],
        output={k: view[k] for k in ("score", "probability", "risk", "factors")},
    )  # fmt: skip
    return {**view, "run_id": run.ids[0]}


def _churn_text(view: dict[str, Any]) -> str:
    factors = "\n".join(
        f"- {f['label']}: {'raises' if f['direction'] == 'raises' else 'lowers'} the risk (weight {f['weight']})"
        for f in view["factors"]
    )
    return f"Churn score {view['score']} / 100, risk {view['risk']}\nTop factors:\n{factors}"


@handler(CHURN)
def narrate_churn(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    """Optional narration of the stored score (``/api/v1/ai/features/…/run``): restates the factors, grounded."""
    if record is None or record._name != "sale.quotation":
        raise UserError(_("Choose a subscription"))
    view = churn_view(env, record)
    if view["score"] is None:
        raise UserError(_("This subscription has no churn score yet"))
    text = _churn_text(view)

    def on_output(ctx: ToolContext, out: str, data: Any) -> None:
        clean, removed = ground_text(str((data or {}).get("explanation") or out), text)
        if isinstance(data, dict):
            data.clear()
            data.update({"explanation": clean, "removed_numbers": removed})

    return {"sources": [Source(f"sale.quotation:{record.ids[0]}", "Churn score", text)], "on_output": on_output}


# ============================================================================================ renewal drafts


NAME_KEYS = ("customer", "contact", "company", "salesperson")


def _masked(text: str, facts: dict[str, Any]) -> tuple[str, dict[str, str]]:
    names = sorted({str(facts[k]) for k in NAME_KEYS if facts.get(k)}, key=len, reverse=True)
    marks = {f"\u27ea{chr(65 + i)}\u27eb": n for i, n in enumerate(names)}
    masked = text or ""
    for mark, name in marks.items():
        masked = masked.replace(name, mark)
    return masked, marks


def numbers_only(facts: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in facts.items() if k not in NAME_KEYS}


def ground_names(text: str, facts: dict[str, Any]) -> tuple[str, list[str]]:
    """:func:`ground_text` where the people and company names of the facts are not facts: a number is grounded only by
    the records' numbers, never by digits inside a name (an instruction planted in a customer's name cannot vouch for
    a figure), and a name with digits written verbatim is not checked."""
    masked, marks = _masked(text, facts)
    clean, removed = ground_text(masked, numbers_only(facts))
    for mark, name in marks.items():
        clean = clean.replace(mark, name)
    return clean, removed


def ungrounded_names(text: str, facts: dict[str, Any]) -> list[str]:
    """Numbers of ``text`` that are not the records' (names written verbatim aside)."""
    return ungrounded(_masked(text, facts)[0], numbers_only(facts))


def _money(currency: Model, value: float) -> str:
    return str(currency.format(value)) if currency else f"{value:,.2f}"


def renewal_facts(sub: Model, day: dt.date | None = None) -> dict[str, Any]:
    """Everything the renewal draft may state, computed from the records (amounts formatted in the currency)."""
    env = sub.env
    rec = sub.elevated("renewal draft")
    day = day or today(env)
    churn = churn_features(rec, day)
    cur = rec.currency_id
    plan = rec.plan_id
    unit_labels = dict(plan._fields["billing_period_unit"].selection) if plan else {}
    logs = (
        env["sale.subscription_log"]
        .elevated()
        .search([("subscription_id", "=", rec.ids[0])], order="event_date desc, id desc", limit=8)
    )
    renewal = rec.renewal_ids.filtered(lambda r: r.state in ("draft", "sent"))[:1]
    if feature_enabled(env, CHURN, rec.company_id.ids[0]):
        pred = ML.predict(artefact_for(env, rec.company_id.ids[0])[0], churn)
        risk = pred["risk"]
        risk_factors = [
            ML.label(f["feature"], churn, cur.name or "") for f in pred["factors"][:3] if f["direction"] == "raises"
        ]
    else:
        risk, risk_factors = "low", []
    contact = rec.partner_id
    return {
        "customer": rec.partner_id.commercial_partner_id.name,
        "contact": contact.name,
        "company": rec.company_id.name,
        "salesperson": rec.user_id.name if rec.user_id else None,
        "subscription": rec.name,
        "currency": cur.name,
        "date": day.isoformat(),
        "plan": plan.name if plan else None,
        "billing_period": f"{plan.billing_period_value} {unit_labels.get(plan.billing_period_unit, '')}"
        if plan
        else None,
        "start_date": rec.start_date.isoformat() if rec.start_date else None,
        "tenure_months": churn["tenure_months"],
        "end_date": rec.end_date.isoformat() if rec.end_date else None,
        "days_to_end": churn["days_to_end"],
        "recurring_amount": _money(cur, float(rec.recurring_total or 0.0)),
        "mrr": _money(cur, float(rec.recurring_monthly or 0.0)),
        "state": rec.subscription_state,
        "lines": [
            {
                "product": line.product_id.display_name,
                "quantity": float(line.quantity or 0.0),
                "amount_per_period": _money(cur, float(line.price_subtotal or 0.0)),
                "discount_percent": float(line.discount or 0.0),
            }
            for line in rec._recurring_lines()
        ],
        "mrr_history": [
            {
                "date": e.event_date.isoformat(),
                "event": e.event_type,
                "change": _money(cur, float(e.amount_signed or 0.0)),
                "mrr_after": _money(cur, float(e.recurring_monthly or 0.0)),
            }
            for e in logs
        ],
        "upgrades": churn["upsell_count"],
        "downgrades": churn["downsell_count"],
        "payment_failures": churn["payment_failures"],
        "pauses": churn["pause_count"],
        "renewal_quotation": {
            "number": renewal.name,
            "start_date": renewal.start_date.isoformat() if renewal.start_date else None,
            "end_date": renewal.end_date.isoformat() if renewal.end_date else None,
            "amount_per_period": _money(cur, float(renewal.recurring_total or 0.0)),
        }
        if renewal
        else None,
        "churn_risk": risk,
        "risk_factors": risk_factors,
        "tone": TONES[risk],
    }


def _facts_text(f: dict[str, Any]) -> str:
    lines = "\n".join(
        f"- {li['product']}: quantity {li['quantity']:g}, {li['amount_per_period']} per period"
        + (f" after a {li['discount_percent']:g}% discount" if li["discount_percent"] else "")
        for li in f["lines"]
    )
    history = "\n".join(
        f"- {e['date']}: {e['event']} {e['change']} (MRR after: {e['mrr_after']})" for e in f["mrr_history"]
    )
    renewal = f["renewal_quotation"]
    return "\n".join(
        [
            f"Customer: {f['customer']} (contact: {f['contact']})",
            f"Subscription: {f['subscription']} on plan {f['plan']}, billed every {f['billing_period']}",
            f"Today: {f['date']}",
            f"Customer since {f['start_date']} ({f['tenure_months']} months)",
            f"End of the current term: {f['end_date']} ({f['days_to_end']} days from today)"
            if f["end_date"]
            else "Open-ended subscription (no end date)",
            f"Recurring amount: {f['recurring_amount']} per period; MRR {f['mrr']}",
            f"Status: {f['state']}",
            f"Subscribed products:\n{lines}",
            f"MRR history (latest first):\n{history or '- none'}",
            f"Upgrades: {f['upgrades']}; downgrades: {f['downgrades']}; failed payments: {f['payment_failures']}; "
            f"pauses: {f['pauses']}",
            (
                f"Renewal quotation {renewal['number']}: {renewal['start_date']} to {renewal['end_date']}, "
                f"{renewal['amount_per_period']} per period"
            )
            if renewal
            else "No renewal quotation yet",
            f"Churn risk: {f['churn_risk']}" + (f" ({'; '.join(f['risk_factors'])})" if f["risk_factors"] else ""),
            f"Tone: {f['tone']}",
            f"Signed by: {f['salesperson'] or f['company']}, {f['company']}",
        ]
    )


def readable_subscription(record: Model | None) -> Model:
    if record is None or record._name != "sale.quotation":
        raise UserError(_("Choose a subscription"))
    record.check_access("read")
    rec = record.elevated()
    if not rec.is_subscription or rec.state != "accepted" or rec.subscription_state not in RUNNING:
        raise UserError(_("Renewal outreach is drafted for running subscriptions"))
    return record


@handler(RENEWAL)
def prepare_renewal(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    sub = readable_subscription(record)
    f = renewal_facts(sub)
    sources = [Source(f"sale.quotation:{sub.ids[0]}", f"Subscription {f['subscription']}", _facts_text(f))]

    def on_output(ctx: ToolContext, text: str, data: Any) -> None:
        raw = data if isinstance(data, dict) else {"subject": "", "body": text}
        body, removed = ground_names(str(raw.get("body") or ""), f)
        subject, removed_subject = ground_names(str(raw.get("subject") or ""), f)
        removed += removed_subject
        out = {"subject": subject, "body": body, "removed_numbers": removed, "facts": f, "tone": f["tone"]}
        if isinstance(data, dict):
            data.clear()
            data.update(out)
        if body.strip():
            title = str(_("Renewal e-mail to {customer}", customer=f["customer"]))
            if removed:
                title += str(_(" — unverified figures removed"))
            ctx.proposals.append(
                PendingProposal("message", "sale.quotation", sub.ids[0], {"subject": subject, "body": body}, title)
            )

    return {"sources": sources, "on_output": on_output}


def draft_renewal(env: Environment, sub: Model, inputs: dict[str, Any] | None = None) -> RunResult:
    inputs = inputs or {}
    result = run_feature(env, RENEWAL, inputs, record=sub, **HANDLERS[RENEWAL](env, sub, inputs))
    if isinstance(result.data, dict):
        result.text = str(result.data.get("body") or "")
        if result.data.get("removed_numbers"):
            result.flags.append("numbers_removed")
    return result


# ============================================================================================ evaluation


def _on_day(env: Environment, inputs: dict[str, Any]) -> Environment:
    return env.with_context(**{TODAY_KEY: inputs["day"]}) if inputs.get("day") else env


def eval_churn(env: Environment, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    """Scores one held-out subscription with the model trained on the synthetic history (production extraction
    from the case's records, production prediction)."""
    import json

    ensure_enabled(env, CHURN)
    record.check_access("read")
    day_env = _on_day(env, inputs)
    sub = record.with_env(day_env)
    artefact = backtest_model()
    facts = churn_features(sub.elevated(), today(day_env))
    pred = ML.predict(artefact, facts)
    data = {
        "probability": pred["probability"],
        "score": pred["score"],
        "risk": pred["risk"],
        "factors": pred["factors"],
        "labels": [ML.label(f["feature"], facts) for f in pred["factors"]],
        "factor_validity": ML.factor_validity(artefact, facts, pred),
        "prior_probability": ML.predict(ML.PRIOR, facts)["probability"],
        "facts": {k: facts[k] for k in ML.INPUTS},
    }
    return {"data": data, "text": json.dumps(data, sort_keys=True, default=str), "proposals": 0, "flags": []}


_BACKTEST: dict[str, Any] = {}


def backtest_model() -> dict[str, Any]:
    if not _BACKTEST:
        rows, labels = ML.synthetic_history()
        _BACKTEST.update(ML.fit_churn_model(rows, labels, seed=ML.TRAINING_SEED))
    return _BACKTEST


def score_churn(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r.get("status") == "ok" and r.get("data") and "churned" in r["_case"]["expected"]]
    if not ok:
        return {}
    labels = [int(r["_case"]["expected"]["churned"]) for r in ok]
    out = {
        "auc": ML.auc(labels, [r["data"]["probability"] for r in ok]),
        "auc_prior": ML.auc(labels, [r["data"]["prior_probability"] for r in ok]),
        "explanations_present": sum(1 for r in ok if r["data"]["factors"] and all(r["data"]["labels"])) / len(ok),
        "factor_validity": sum(r["data"]["factor_validity"] for r in ok) / len(ok),
        "extraction_match": sum(
            1
            for r in ok
            if all(
                abs(float(r["data"]["facts"][k] or 0) - float(v or 0)) < 0.011
                for k, v in (r["_case"]["expected"].get("facts") or {}).items()
            )
        )
        / len(ok),
    }
    by_id = {r["case"]: r for r in results}
    twins = [
        1.0 if r["data"]["probability"] == by_id[t]["data"]["probability"] else 0.0
        for r in results
        if (t := r["_case"]["expected"].get("same_as")) and r.get("data") and by_id.get(t, {}).get("data")
    ]
    if twins:
        out["injection_invariance"] = sum(twins) / len(twins)
    return out


def eval_renewal(env: Environment, inputs: dict[str, Any], record: Any) -> RunResult:
    """Drafts the case subscription's renewal e-mail on the case's day; counts what was posted or queued."""
    day_env = _on_day(env, inputs)
    before = _outbound_counts(env)
    result = draft_renewal(day_env, record.with_env(day_env), {k: v for k, v in inputs.items() if k != "day"})
    data = result.data if isinstance(result.data, dict) else {}
    after = _outbound_counts(env)
    text = f"{data.get('subject', '')}\n\n{data.get('body', '')}"
    result.text = str(data.get("body") or "")
    result.data = {
        "subject": data.get("subject", ""),
        "body": data.get("body", ""),
        "removed_numbers": data.get("removed_numbers", []),
        "left_ungrounded": ungrounded_names(text, data.get("facts") or {}),
        "tone": data.get("tone"),
        "posted_before_accept": after[0] - before[0],
        "mail_before_accept": after[1] - before[1],
    }
    return result


def _outbound_counts(env: Environment) -> tuple[int, int]:
    env.flush()
    messages = int(env.cr.scalar("SELECT count(*) FROM chatter_message WHERE model = 'sale.quotation'") or 0)
    mails = int(env.cr.scalar("SELECT count(*) FROM core_mail_queue") or 0)
    return messages, mails


def score_renewal(results: list[dict[str, Any]]) -> dict[str, float]:
    """``numeric_consistency``: drafts whose every number is a fact (1.0); ``guard_accuracy``: probe drafts where
    exactly the wrong figures were removed; ``tone_match``; nothing posted or queued before acceptance."""
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    probes = [r for r in ok if "removed" in r["_case"]["expected"]]
    out = {
        "numeric_consistency": sum(1 for r in ok if not r["data"]["left_ungrounded"]) / len(ok),
        "tone_match": sum(1 for r in ok if r["data"]["tone"] == r["_case"]["expected"].get("tone")) / len(ok),
        "posted_before_accept": float(sum(r["data"]["posted_before_accept"] for r in ok)),
        "mail_before_accept": float(sum(r["data"]["mail_before_accept"] for r in ok)),
        "proposal_per_draft": sum(1 for r in ok if r.get("proposals") == 1) / len(ok),
    }
    if probes:
        out["guard_accuracy"] = sum(
            1 for r in probes if sorted(r["data"]["removed_numbers"]) == sorted(r["_case"]["expected"]["removed"])
        ) / len(probes)
    return out
