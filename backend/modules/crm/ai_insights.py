"""CRM insights (Phase 1.6): predictive lead scoring, next-best-action, deal risk, lead routing and the
forecast with confidence ranges.

* ``crm.lead_score``, ``crm.deal_risk``, ``crm.forecast`` and ``crm.lead_routing`` compute their numbers
  in-process (classical ML, rules and statistics — AI architecture note §4.7); every computation shown to
  a person is recorded as an ``ai_core.run`` so it can be audited and rated.
* ``crm.next_best_action`` is the LLM feature: the model ranks actions from the lead, its conversations
  and the deterministic signals computed here; scheduling one is an ``action`` proposal.
* Routing never assigns by itself: it proposes, unless an assignment rule explicitly allows auto-apply.

The numeric core lives in :mod:`modules.crm.ai_insights_ml`; the HTTP API in
:mod:`modules.crm.ai_insights_api`; evaluation entry points in :mod:`modules.crm.ai_insights_eval`.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from typing import TYPE_CHECKING, Any

from modules.crm import ai_insights_ml as ML
from tessera.exceptions import AccessError, UserError
from tessera.jobs import enqueue, job
from tessera.logging import get_logger
from tessera.orm.hooks import hook

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)

SCORE = "crm.lead_score"
NBA = "crm.next_best_action"
RISK = "crm.deal_risk"
ROUTING = "crm.lead_routing"
FORECAST = "crm.forecast"
PROVIDER = "Tessera (in-process)"
ACTIVITY_TYPES = ("comment", "note", "email", "activity_done")
SCORE_TRIGGERS = {
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
    "active",
    "lost",
    "stage_id",
}


# ----------------------------------------------------------------------------- platform helpers


def ai_installed(env: Environment) -> bool:
    return "ai_core.feature" in env.registry.models


def feature_enabled(env: Environment, name: str, company_id: int | None) -> bool:
    if not ai_installed(env):
        return False
    feature = env["ai_core.feature"].elevated().search([("name", "=", name)], limit=1)
    return bool(feature) and feature.enabled_for(company_id)


def ensure_enabled(env: Environment, name: str) -> Model:
    """Kill switches and internal-user check (raises ``FeatureDisabled`` / ``AccessError``)."""
    from tessera.ai.errors import FeatureDisabled
    from tessera.ai.runtime import check_enabled, get_feature

    if not ai_installed(env):
        raise FeatureDisabled("AI is not installed")
    feature = get_feature(env, name)
    check_enabled(env, feature)
    return feature


def audit_run(
    env: Environment,
    name: str,
    *,
    record: Model | None = None,
    inputs: dict[str, Any] | None = None,
    output: Any = None,
    text: str = "",
    model: str = "",
    latency_ms: int = 0,
    company_id: int | None = None,
) -> Model:
    """An ``ai_core.run`` for a computation that made no provider call (audit trail and feedback)."""
    feature = env["ai_core.feature"].elevated().search([("name", "=", name)], limit=1)
    company = company_id or (env.company.ids[0] if env.company else None)
    return (
        env["ai_core.run"]
        .elevated("ai run")
        .create(
            {
                "feature_id": feature.ids[0] if feature else None,
                "feature": name,
                "user_id": env.uid,
                "company_id": company,
                "provider": PROVIDER,
                "model": model,
                "status": "ok",
                "res_model": record._name if record is not None else None,
                "res_id": record.ids[0] if record is not None and record.ids else None,
                "input_ref": {"inputs": json.loads(json.dumps(inputs or {}, default=str))},
                "output_text": text,
                "output_data": json.loads(json.dumps(output, default=str)) if output is not None else None,
                "latency_ms": latency_ms,
                "cost": 0.0,
                "steps": 0,
            }
        )
    )


def _write_ai(lead: Model, vals: dict[str, Any]) -> None:
    """System write of AI fields: no rescoring, no tracking, no re-trigger of the AI jobs."""
    lead.elevated("ai insights").with_context(crm_scoring=True, tracking_disable=True, crm_ai=True).write(vals)


def _notify(env: Environment, user_ids: list[int], title: str, body: str, *, url: str, model: str = "", res_id: int = 0) -> None:
    users = [u for u in dict.fromkeys(user_ids) if u]
    if users:
        env["core.notification"].notify(users, title, body, kind="job", model=model or None, res_id=res_id or None, url=url)


def _companies(env: Environment, company_ids: list[int] | None) -> list[int]:
    if company_ids:
        return list(company_ids)
    return list(env["core.company"].elevated().search([]).ids)


def _managers(env: Environment, company_id: int) -> list[int]:
    group = env.ref("crm.group_crm_manager")
    users = env["core.user"].elevated().search([("group_ids", "in", group.ids), ("company_ids", "in", [company_id])])
    from tessera.orm.environment import SYSTEM_UID

    return [u for u in users.ids if u != SYSTEM_UID]


# ----------------------------------------------------------------------------- lead scoring


def _get(record: Model, field: str) -> Any:
    return record[ML.assert_allowed(field)]


def message_counts(env: Environment, ids: list[int], cutoffs: dict[int, Any] | None = None) -> dict[int, int]:
    """Logged conversations per lead (before each lead's cutoff date, e.g. its closing date)."""
    if not ids:
        return {}
    ML.assert_allowed("chatter.message")
    env.flush()
    rows = env.cr.fetchall(
        "SELECT res_id, date FROM chatter_message WHERE model = 'crm.lead' AND res_id = ANY(:ids)"
        " AND message_type = ANY(:types)",
        {"ids": list(ids), "types": list(ACTIVITY_TYPES)},
    )
    out = dict.fromkeys(ids, 0)
    for res_id, date in rows:
        cutoff = (cutoffs or {}).get(res_id)
        if cutoff is None or date is None or date <= cutoff:
            out[res_id] += 1
    return out


def lead_values(lead: Model, messages: int | None = None) -> dict[str, Any]:
    """Normalised scoring inputs of one lead. Every lead field goes through the allow-list guard, so a
    protected attribute (or proxy) cannot become a model input by accident."""
    rec = lead.elevated().with_context(active_test=False)
    partner = _get(rec, "partner_id")
    tags = _get(rec, "tag_ids")
    source = _get(rec, "source_id")
    if messages is None:
        cutoff = rec.date_closed if (rec.is_won or rec.lost) else None
        messages = message_counts(lead.env, [rec.ids[0]], {rec.ids[0]: cutoff})[rec.ids[0]]
    return {
        "expected_revenue": float(_get(rec, "expected_revenue") or 0.0),
        "has_company": bool(_get(rec, "partner_name")) or bool(partner and partner.commercial_partner_id.is_company),
        "existing_customer": bool(partner),
        "has_phone": bool(_get(rec, "phone") or _get(rec, "mobile")),
        "email_from": _get(rec, "email_from") or None,
        "website": _get(rec, "website") or None,
        "priority": int(_get(rec, "priority") or 0),
        "source": source.name if source else None,
        "tags": sorted(tags.mapped("name")),
        "messages": int(messages),
    }


def training_set(env: Environment, company_id: int) -> tuple[list[dict[str, Any]], list[int]]:
    """The company's won and lost deals (last 24 months), as scoring inputs and labels."""
    since = env.now() - dt.timedelta(days=730)
    leads = (
        env["crm.lead"]
        .elevated()
        .with_context(active_test=False)
        .search(
            [
                ("company_id", "=", company_id),
                ("date_closed", ">=", since),
                {"any": [["is_won", "=", True], ["lost", "=", True]]},
            ]
        )
    )
    counts = message_counts(env, list(leads.ids), {lead.ids[0]: lead.date_closed for lead in leads})
    rows, labels = [], []
    for lead in leads:
        rows.append(lead_values(lead, counts[lead.ids[0]]))
        labels.append(1 if lead.is_won else 0)
    return rows, labels


def train_company_model(env: Environment, company_id: int) -> Model | None:
    """Train and activate a new model version, or ``None`` while history is too thin (cold start)."""
    rows, labels = training_set(env, company_id)
    try:
        artefact = ML.fit_lead_model(rows, labels, seed=company_id)
    except ValueError:
        return None
    models = env["crm.ai_score_model"].elevated()
    previous = models.search([("company_id", "=", company_id)], order="version desc", limit=1)
    models.search([("company_id", "=", company_id), ("state", "=", "active")]).write({"state": "retired"})
    return models.create(
        {
            "company_id": company_id,
            "version": (previous.version if previous else 0) + 1,
            "algorithm": artefact["algorithm"],
            "feature_names": artefact["features"],
            "artefact": artefact,
            "artefact_hash": artefact["hash"],
            "metrics": artefact["metrics"],
            "trained_at": env.now(),
        }
    )


def active_model(env: Environment, company_id: int | None) -> Model | None:
    if not company_id:
        return None
    model = (
        env["crm.ai_score_model"]
        .elevated()
        .search([("company_id", "=", company_id), ("state", "=", "active")], order="version desc", limit=1)
    )
    if not model:
        return None
    if ML.artefact_hash(model.artefact or {}) != model.artefact_hash:
        log.warning("lead scoring artefact hash mismatch; falling back to rules", model=model.ids[0])
        return None
    return model


def rule_prediction(lead: Model) -> dict[str, Any]:
    """Cold start: the rule-based score of ``crm.scoring_rule`` read as a probability."""
    rec = lead.elevated()
    score = max(0, min(100, int(rec.score or 0)))
    factors = [
        {
            "feature": f"rule:{f.get('rule_id')}",
            "label": f.get("rule", ""),
            "value": 1.0,
            "direction": "positive" if f.get("points", 0) >= 0 else "negative",
            "weight": round(float(f.get("points", 0)) / 100.0, 4),
        }
        for f in sorted(rec.score_factors or [], key=lambda f: -abs(f.get("points", 0)))[:5]
    ]
    return {"probability": round(score / 100.0, 4), "score": score, "factors": factors}


def score_leads(leads: Model) -> dict[str, int]:
    """Score open leads in-process with their company's model (rules while it has none) and store the
    score, probability and top-5 factors on the lead."""
    env = leads.env
    sys_leads = leads.elevated().with_context(active_test=False).exists()
    open_leads = sys_leads.filtered(lambda lead: lead.active and not lead.is_won and not lead.lost)
    counts = message_counts(env, list(open_leads.ids))
    done = {"model": 0, "rules": 0, "skipped": 0}
    models: dict[int | None, Model | None] = {}
    now = env.now()
    for lead in open_leads:
        cid = lead.company_id.ids[0] if lead.company_id else None
        if not feature_enabled(env, SCORE, cid):
            done["skipped"] += 1
            continue
        if cid not in models:
            models[cid] = active_model(env, cid)
        model = models[cid]
        if model is not None:
            pred = ML.predict_lead(model.artefact, lead_values(lead, counts[lead.ids[0]]))
            source = "model"
        else:
            pred = rule_prediction(lead)
            source = "rules"
        _write_ai(
            lead,
            {
                "ai_score": pred["score"],
                "ai_probability": pred["probability"],
                "ai_score_factors": pred["factors"],
                "ai_score_source": source,
                "ai_score_model_id": model.ids[0] if model is not None else None,
                "ai_scored_at": now,
            },
        )
        done[source] += 1
    return done


@job("crm.ai_train_score_models", queue="ai", max_attempts=2, as_system=True, timeout_seconds=3600)
def train_score_models_job(
    env: Any, company_ids: list[int] | None = None, notify_user_id: int | None = None
) -> dict[str, Any]:
    """Nightly: retrain each company's model on its won/lost history, then rescore its open leads."""
    out: dict[str, Any] = {}
    for cid in _companies(env, company_ids):
        if not feature_enabled(env, SCORE, cid):
            continue
        model = train_company_model(env, cid)
        leads = env["crm.lead"].elevated().search([("company_id", "=", cid), ("is_won", "=", False)])
        counts = score_leads(leads)
        out[str(cid)] = {"version": model.version if model else None, **counts}
        env.flush()
        text = (
            f"Lead scoring model v{model.version} trained on {model.metrics['rows']} closed deals "
            f"(hold-out AUC {model.metrics['holdout_auc']:.2f}); {counts['model']} open leads rescored."
            if model
            else f"Not enough closed deals for a predictive model yet; {counts['rules']} leads scored with the rules."
        )
        recipients = [notify_user_id] if notify_user_id else (_managers(env, cid) if model else [])
        _notify(env, recipients, "Lead scores updated", text, url="/a/crm.action_leads")
    return out


@job("crm.ai_score_leads", queue="ai", max_attempts=3, as_system=True)
def score_leads_job(env: Any, ids: list[int]) -> dict[str, int]:
    return score_leads(env["crm.lead"].browse(ids))


def _crm_ai_jobs_ready(records: Model) -> bool:
    env = records.env
    return (
        records._name == "crm.lead"
        and not env.context.get("crm_ai")
        and "core.job" in env.registry.models
        and ai_installed(env)
        and "crm.ai_score_model" in env.registry.models
    )


@hook("after_create")
def score_new_leads(records: Any, vals_list: list[dict[str, Any]]) -> None:
    if _crm_ai_jobs_ready(records):
        enqueue(records.env.elevated(), "crm.ai_score_leads", {"ids": list(records.ids)}, queue="ai")


@hook("after_write")
def rescore_changed_leads(records: Any, vals: dict[str, Any], old_values: dict[Any, Any]) -> None:
    if _crm_ai_jobs_ready(records) and set(vals) & SCORE_TRIGGERS:
        enqueue(records.env.elevated(), "crm.ai_score_leads", {"ids": list(records.ids)}, queue="ai")


# ----------------------------------------------------------------------------- deal risk


def deal_signals(lead: Model, now: dt.datetime | None = None) -> dict[str, Any]:
    """Signals of an open deal as of ``now``: last conversation, stage age, next activity, closing
    date and negative wording in the conversations of the last 60 days."""
    from tessera.ai.prompts import html_to_text

    env = lead.env
    rec = lead.elevated()
    now = now or env.now()
    today = now.date()
    msgs = (
        env["chatter.message"]
        .elevated()
        .search(
            [("model", "=", "crm.lead"), ("res_id", "=", rec.ids[0]), ("message_type", "in", list(ACTIVITY_TYPES))],
            order="date desc",
            limit=50,
        )
    )
    msgs = msgs.filtered(lambda m: m.date is None or m.date <= now)
    last = max((m.date for m in msgs if m.date), default=None)
    if last is None:
        last = rec.date_last_stage_update or rec.create_date
    recent = [html_to_text(m.body or "") for m in msgs if m.date and (now - m.date).days <= 60]
    acts = (
        env["chatter.activity"]
        .elevated()
        .search([("model", "=", "crm.lead"), ("res_id", "=", rec.ids[0]), ("done", "=", False)], order="date_deadline")
    )
    next_deadline = acts[:1].date_deadline if acts else None
    stage_since = rec.date_last_stage_update or rec.create_date
    return {
        "days_since_activity": max(0, (now - last).days) if last else None,
        "stage_age_days": max(0, (now - stage_since).days) if stage_since else 0,
        "has_next_activity": bool(acts),
        "next_activity_overdue_days": max(0, (today - next_deadline).days) if next_deadline else 0,
        "days_past_close": (today - rec.date_deadline).days if rec.date_deadline else None,
        "negative_phrases": ML.negative_phrases(recent),
    }


def assess_deal(lead: Model, now: dt.datetime | None = None) -> dict[str, Any]:
    signals = deal_signals(lead, now)
    return {**ML.deal_risk(signals), "signals": signals}


def open_deals(env: Environment, company_id: int) -> Model:
    return (
        env["crm.lead"]
        .elevated()
        .search([("company_id", "=", company_id), ("type", "=", "opportunity"), ("is_won", "=", False)])
    )


@job("crm.ai_deal_risk", queue="ai", max_attempts=2, as_system=True, timeout_seconds=3600)
def deal_risk_job(env: Any, company_ids: list[int] | None = None, notify_user_id: int | None = None) -> dict[str, Any]:
    """Daily: flag open opportunities at risk. Notification only (salesperson of each newly flagged
    deal, and whoever asked for the run); nothing about the deal itself changes."""
    out: dict[str, Any] = {}
    now = env.now()
    for cid in _companies(env, company_ids):
        if not feature_enabled(env, RISK, cid):
            continue
        flagged = newly = 0
        for lead in open_deals(env, cid):
            before = lead.ai_risk_level
            risk = assess_deal(lead, now)
            _write_ai(
                lead,
                {
                    "ai_risk_level": risk["level"],
                    "ai_risk_score": risk["score"],
                    "ai_risk_reasons": risk["reasons"],
                    "ai_risk_checked_at": now,
                },
            )
            if risk["flagged"]:
                flagged += 1
                if before not in ("medium", "high"):
                    newly += 1
                    top = "; ".join(r["label"] for r in risk["reasons"][:2])
                    _notify(
                        env,
                        [lead.user_id.ids[0]] if lead.user_id else [],
                        f"Deal at risk: {lead.name}",
                        top,
                        url=f"/m/crm.lead/{lead.ids[0]}",
                        model="crm.lead",
                        res_id=lead.ids[0],
                    )
        out[str(cid)] = {"flagged": flagged, "newly_flagged": newly}
        if notify_user_id:
            _notify(
                env,
                [notify_user_id],
                "Deal risk check finished",
                f"{flagged} open opportunities are at risk ({newly} new).",
                url="/a/crm.action_pipeline",
            )
    return out


# ----------------------------------------------------------------------------- lead routing


def _open_load(env: Environment, user_ids: list[int], company_id: int | None) -> dict[int, int]:
    if not user_ids:
        return {}
    env.flush()
    rows = env.cr.fetchall(
        "SELECT user_id, count(*) FROM crm_lead WHERE user_id = ANY(:u) AND active AND NOT coalesce(is_won, false)"
        " AND (CAST(:c AS integer) IS NULL OR company_id = :c) GROUP BY user_id",
        {"u": list(user_ids), "c": company_id},
    )
    return {**dict.fromkeys(user_ids, 0), **{r[0]: int(r[1]) for r in rows}}


def _similar_history(env: Environment, lead: Model, user_ids: list[int]) -> dict[int, tuple[int, int]]:
    """(won, closed) per user over closed deals similar to ``lead``: a shared tag, else the same source."""
    if not user_ids:
        return {}
    tags = set(lead.tag_ids.ids)
    source = lead.source_id.ids[0] if lead.source_id else None
    closed = (
        env["crm.lead"]
        .elevated()
        .with_context(active_test=False)
        .search(
            [
                ("user_id", "in", list(user_ids)),
                ("id", "!=", lead.ids[0]),
                {"any": [["is_won", "=", True], ["lost", "=", True]]},
            ]
        )
    )
    out = {u: (0, 0) for u in user_ids}
    for c in closed:
        similar = bool(tags & set(c.tag_ids.ids)) if tags else (source is not None and c.source_id.ids[:1] == [source])
        if not similar:
            continue
        won, total = out[c.user_id.ids[0]]
        out[c.user_id.ids[0]] = (won + (1 if c.is_won else 0), total + 1)
    return out


def _best_team(env: Environment, lead: Model) -> Model:
    """No rule and no team: the team that handled the most similar closed deals (else the first team)."""
    teams = env["crm.team"].elevated().search([("active", "=", True)])
    if lead.company_id:
        teams = teams.filtered(lambda t: not t.company_id or t.company_id == lead.company_id)
    best, best_n = teams[:1], -1
    tags = set(lead.tag_ids.ids)
    for team in teams:
        domain: list[Any] = [("team_id", "=", team.ids[0])]
        if tags:
            domain.append(("tag_ids", "in", list(tags)))
        elif lead.source_id:
            domain.append(("source_id", "=", lead.source_id.ids[0]))
        n = env["crm.lead"].elevated().with_context(active_test=False).search_count(domain)
        if n > best_n:
            best, best_n = team, n
    return best


def suggest_assignee(lead: Model) -> dict[str, Any]:
    """Suggested team and salesperson for one lead, with reasons: the assignment rules first (as the
    automatic assignment applies them), then within the team the member who balances proven success on
    similar deals against current open load. Read-only."""
    from modules.crm.services import _matches

    env = lead.env
    rec = lead.elevated()
    reasons: list[str] = []
    team = rec.team_id
    user = env["core.user"]
    rule_used = None
    rules = env["crm.assignment_rule"].elevated().search([("active", "=", True)], order="sequence, id")
    for rule in rules:
        if rule.team_id and rule.team_id.ids != team.ids:
            continue
        if rec.ids[0] not in _matches(rec, rule.domain):
            continue
        rule_used = rule
        if rule.assign_team_id:
            team = rule.assign_team_id
            reasons.append(f"Rule “{rule.name}” routes it to {team.name}")
        if rule.assign_user_id:
            user = rule.assign_user_id
            reasons.append(f"Rule “{rule.name}” assigns {user.name}")
            break
    candidates: list[dict[str, Any]] = []
    if not user:
        if not team:
            team = _best_team(env, rec)
            if team:
                reasons.append(f"Similar past deals were handled by {team.name}")
        members = (
            env["crm.team_member"].elevated().search([("team_id", "=", team.ids[0]), ("active", "=", True)])
            if team
            else env["crm.team_member"]
        )
        eligible = [m for m in members if not m.assignment_domain or rec.ids[0] in _matches(rec, m.assignment_domain)]
        uids = [m.user_id.ids[0] for m in eligible]
        cid = rec.company_id.ids[0] if rec.company_id else None
        load = _open_load(env, uids, cid)
        hist = _similar_history(env, rec, uids)
        won_all = sum(w for w, _ in hist.values())
        closed_all = sum(n for _, n in hist.values())
        team_rate = (won_all + 1) / (closed_all + 2)
        loads = {m.user_id.ids[0]: load[m.user_id.ids[0]] / max(m.assignment_weight or 1, 1) for m in eligible}
        mean_load = sum(loads.values()) / len(loads) if loads else 0.0
        for m in eligible:
            uid = m.user_id.ids[0]
            won, closed = hist[uid]
            rate = (won + 1) / (closed + 2)
            expertise = 2.0 * min(1.0, closed / 4.0) * (rate - team_rate)
            balance = -0.6 * (loads[uid] - mean_load) / max(1.0, mean_load)
            candidates.append(
                {
                    "user_id": uid,
                    "name": m.user_id.name,
                    "score": round(expertise + balance, 4),
                    "won_similar": won,
                    "closed_similar": closed,
                    "open_leads": load[uid],
                    "last": m.last_assigned_at.isoformat() if m.last_assigned_at else "",
                }
            )
        candidates.sort(key=lambda c: (-c["score"], c["last"], c["user_id"]))
        if candidates:
            best = candidates[0]
            user = env["core.user"].elevated().browse([best["user_id"]])
            if best["closed_similar"]:
                reasons.append(f"{best['name']} won {best['won_similar']} of {best['closed_similar']} similar deals")
            reasons.append(
                f"{best['name']} has {best['open_leads']} open leads (team average {mean_load:.0f})"
                if len(candidates) > 1
                else f"{best['name']} is the only eligible member"
            )
    confidence = 1.0 if rule_used is not None and rule_used.assign_user_id else 0.5
    if len(candidates) > 1:
        margin = candidates[0]["score"] - candidates[1]["score"]
        confidence = round(min(0.95, 0.5 + margin), 2)
    return {
        "user_id": user.ids[0] if user else None,
        "user": user.name if user else None,
        "team_id": team.ids[0] if team else None,
        "team": team.name if team else None,
        "rule_id": rule_used.ids[0] if rule_used is not None else None,
        "auto_apply": bool(rule_used is not None and rule_used.ai_auto_apply and user),
        "confidence": confidence,
        "reasons": reasons,
        "candidates": candidates[:5],
    }


def propose_routing(env: Environment, leads: Model) -> dict[str, Any]:
    """Routing suggestions for ``leads`` as *write proposals* (one per lead, decided row by row).
    A suggestion is applied directly only when the rule it follows has ``ai_auto_apply`` set."""
    from tessera.ai import proposals as P
    from tessera.ai.tools import PendingProposal

    ensure_enabled(env, ROUTING)
    started = time.monotonic()
    leads.check_access("read")
    single = leads if len(leads) == 1 else None
    rows: list[dict[str, Any]] = []
    pending: list[tuple[Model, dict[str, Any], dict[str, Any]]] = []
    for lead in leads:
        s = suggest_assignee(lead)
        row = {"lead_id": lead.ids[0], "lead": lead.elevated().name, **s, "status": "proposed", "proposal": None}
        rows.append(row)
        if not s["user_id"]:
            row["status"] = "no_candidate"
            continue
        payload: dict[str, Any] = {"user_id": s["user_id"]}
        if s["team_id"] and lead.elevated().team_id.ids[:1] != [s["team_id"]]:
            payload["team_id"] = s["team_id"]
        if lead.elevated().user_id.ids[:1] == [s["user_id"]] and "team_id" not in payload:
            row["status"] = "already_assigned"
            continue
        if s["auto_apply"]:
            try:
                with env.cr.savepoint():
                    lead.write(payload)
                row["status"] = "applied"
                continue
            except (AccessError, UserError):
                row["auto_apply"] = False  # the user may not assign it: fall back to a proposal
        pending.append((lead, payload, row))
    run = audit_run(
        env,
        ROUTING,
        record=single,
        inputs={"lead_ids": list(leads.ids)},
        output=[{k: r[k] for k in ("lead_id", "user_id", "team_id", "status", "reasons")} for r in rows],
        model="rules + load + similar-deal history",
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    from tessera.ai.runtime import proposal_dict

    for lead, payload, row in pending:
        title = f"Assign {lead.elevated().name} to {row['user']}"
        if "team_id" in payload:
            title += f" ({row['team']})"
        p = P.create(env, run, ROUTING, PendingProposal("write", "crm.lead", lead.ids[0], payload, title))
        row["proposal"] = proposal_dict(p)
    if pending:
        run.elevated().write({"decision": "pending"})
    return {"run_id": run.ids[0], "rows": rows, "ai_generated": True}


# ----------------------------------------------------------------------------- next-best-action


def lead_brief(lead: Model) -> str:
    """Plain-text facts about a lead for the next-best-action prompt (data, never instructions)."""
    from tessera.ai.prompts import html_to_text

    rec = lead.elevated()
    env = lead.env
    sig = deal_signals(lead)
    risk = ML.deal_risk(sig)
    lines = [
        f"Name: {rec.name}",
        f"Type: {rec.type}; stage: {rec.stage_id.name or '—'} for {sig['stage_age_days']} days",
        f"Customer: {rec.partner_id.display_name or rec.partner_name or '—'}",
        f"Salesperson: {rec.user_id.name or 'unassigned'}",
        f"Expected revenue: {rec.expected_revenue or 0:,.0f}; probability {rec.probability or 0:.0f}%",
        f"Expected closing: {rec.date_deadline.isoformat() if rec.date_deadline else 'not set'}",
        f"Quotations: {rec.quotation_count}",
    ]
    if rec.ai_score_factors:
        lines.append(
            f"AI score {rec.ai_score}: " + "; ".join(f"{f['label']} ({f['direction']})" for f in rec.ai_score_factors)
        )
    lines.append(
        "Signals: "
        + (
            "; ".join(f"[{r['code']}] {r['label']}" for r in risk["reasons"])
            if risk["reasons"]
            else "[healthy] no risk signal"
        )
    )
    acts = (
        env["chatter.activity"]
        .elevated()
        .search([("model", "=", "crm.lead"), ("res_id", "=", rec.ids[0]), ("done", "=", False)], limit=5)
    )
    for a in acts:
        lines.append(f"Planned: {a.summary or a.activity_type_id.name} due {a.date_deadline}")
    msgs = (
        env["chatter.message"]
        .elevated()
        .search(
            [("model", "=", "crm.lead"), ("res_id", "=", rec.ids[0]), ("message_type", "in", list(ACTIVITY_TYPES))],
            order="date desc",
            limit=8,
        )
    )
    for m in reversed(list(msgs)):
        who = m.author_id.name if m.author_id else (m.email_from or "customer")
        lines.append(f"Message {m.date.date() if m.date else ''} from {who}: {html_to_text(m.body or '')[:600]}")
    notes = html_to_text(rec.description or "")[:1500]
    if notes:
        lines.append(f"Notes: {notes}")
    return "\n".join(lines)


SCHEDULABLE = {"call", "email", "meeting", "todo"}


def _nba_output(lead_id: int, today: dt.date) -> Any:
    from tessera.ai.tools import PendingProposal

    def hook(ctx: Any, text: str, data: Any) -> None:
        actions = (data or {}).get("actions") if isinstance(data, dict) else None
        for a in (actions or [])[:3]:
            if not isinstance(a, dict) or a.get("kind") not in SCHEDULABLE:
                continue
            due = today + dt.timedelta(days=max(0, min(30, int(a.get("due_in_days") or 0))))
            title = str(a.get("title") or "Follow up")[:200]
            ctx.proposals.append(
                PendingProposal(
                    "action",
                    "crm.lead",
                    lead_id,
                    {
                        "action": "chatter.schedule_activity",
                        "args": {"summary": title, "date_deadline": due.isoformat()},
                    },
                    f"Schedule: {title}",
                )
            )

    return hook


def nba_context(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    from tessera.ai import Source

    if record is None or record._name != "crm.lead":
        raise UserError("Next-best-action works on a lead or opportunity")
    record.check_access("read")
    rec = record.elevated()
    return {
        "sources": [Source(f"crm.lead:{rec.ids[0]}", rec.name, lead_brief(record))],
        "on_output": _nba_output(rec.ids[0], env.today()),
    }


def score_narration_context(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    """Optional narration of a lead's score factors (the number itself never comes from the model)."""
    from tessera.ai import Source

    if record is None or record._name != "crm.lead":
        raise UserError("Lead scores are explained on a lead")
    record.check_access("read")
    rec = record.elevated()
    factors = "\n".join(
        f"- {f['label']}: {f['direction']} (weight {f['weight']})" for f in rec.ai_score_factors or []
    )
    text = f"Score {rec.ai_score} / 100, win probability {rec.ai_probability or 0:.0%}\n{factors}"
    return {"sources": [Source(f"crm.lead:{rec.ids[0]}", rec.name, text)]}


def _register_handlers() -> None:
    from tessera.ai.runtime import handler

    handler(NBA)(nba_context)
    handler(SCORE)(score_narration_context)


_register_handlers()


# ----------------------------------------------------------------------------- forecast


def _month_start(d: dt.date, add: int = 0) -> dt.date:
    m = d.month - 1 + add
    return dt.date(d.year + m // 12, m % 12 + 1, 1)


def _months_between(a: dt.date, b: dt.date) -> int:
    return (b.year - a.year) * 12 + (b.month - a.month)


def forecast_inputs(
    env: Environment, company_id: int, team_id: int | None = None, periods: int = 3
) -> dict[str, Any]:
    """Pipeline and history of one company (and team) in the shape :func:`ML.forecast` takes."""
    today = env.today()
    current = _month_start(today)
    leads = env["crm.lead"].elevated().with_context(active_test=False)
    scope: list[Any] = [("company_id", "=", company_id), ("type", "=", "opportunity")]
    if team_id:
        scope.append(("team_id", "=", team_id))
    stages = env["crm.stage"].elevated().search([("is_won", "=", False)], order="sequence, id")
    pipeline = []
    for d in leads.search([*scope, ("active", "=", True), ("is_won", "=", False), ("date_deadline", "!=", None)]):
        k = max(0, _months_between(current, d.date_deadline))
        if k < periods and d.stage_id:
            pipeline.append(
                {"id": d.ids[0], "name": d.name, "amount": float(d.expected_revenue or 0), "stage": d.stage_id.name, "period": k}
            )
    since = dt.datetime.combine(_month_start(today, -12), dt.time(), dt.UTC)
    closed = leads.search([*scope, ("date_closed", ">=", since), {"any": [["is_won", "=", True], ["lost", "=", True]]}])
    stage_history: dict[str, Any] = {}
    for s in stages:
        months: dict[int, dict[str, float]] = {}
        for c in closed:
            age = max(0, _months_between(c.date_closed.date(), today))
            row = months.setdefault(age, {"age_months": age, "won": 0, "lost": 0})
            if c.is_won:
                row["won"] += 1
            elif c.stage_id and c.stage_id.sequence >= s.sequence:
                row["lost"] += 1
        stage_history[s.name] = {"prior": (s.probability or 10.0) / 100.0, "months": list(months.values())}
    slip: dict[int, dict[str, float]] = {}
    run_rate: dict[int, float] = {}
    committed = [0.0] * periods
    for c in closed.filtered(lambda c: c.is_won):
        closed_on = c.date_closed.date()
        age = _months_between(closed_on, today)
        if age == 0:
            committed[0] += float(c.expected_revenue or 0)
            continue
        if c.date_deadline:
            row = slip.setdefault(age, {"age_months": age, "slipped": 0, "on_time": 0})
            row["slipped" if _months_between(c.date_deadline, closed_on) > 0 else "on_time"] += 1
        if c.create_date and _months_between(c.create_date.date(), closed_on) == 0:
            run_rate[age] = run_rate.get(age, 0.0) + float(c.expected_revenue or 0)
    history = [run_rate.get(age, 0.0) for age in range(1, 13)] if run_rate else []
    labels = [_month_start(today, k).strftime("%B %Y") for k in range(periods)]
    return {
        "pipeline": pipeline,
        "stage_history": stage_history,
        "slip_history": list(slip.values()),
        "run_rate_history": history,
        "committed": committed,
        "labels": labels,
        "unscheduled": leads.search_count([*scope, ("active", "=", True), ("is_won", "=", False), ("date_deadline", "=", None)]),
    }


def compute_forecast(
    env: Environment, company_id: int, team_id: int | None = None, *, periods: int = 3, narrative: bool = False
) -> Model:
    from tessera.orm.environment import SYSTEM_UID

    started = time.monotonic()
    data = forecast_inputs(env, company_id, team_id, periods)
    company = env["core.company"].elevated().browse([company_id])
    currency = company.currency_id.name if company.currency_id else ""
    rows = ML.forecast(
        data["pipeline"],
        data["stage_history"],
        slip_history=data["slip_history"],
        run_rate_history=data["run_rate_history"],
        committed=data["committed"],
        periods=periods,
        seed=company_id * 1000 + env.today().toordinal() % 1000,
    )
    for row, label in zip(rows, data["labels"], strict=True):
        row["label"] = label
        row["explanation"] = ML.explain_period(row, label, currency)
    team = env["crm.team"].elevated().browse([team_id]) if team_id else None
    record = (
        env["crm.ai_forecast"]
        .elevated()
        .create(
            {
                "name": f"Forecast {data['labels'][0]}" + (f" — {team.name}" if team else ""),
                "company_id": company_id,
                "team_id": team_id,
                "user_id": env.uid if env.uid != SYSTEM_UID else None,
                "periods": rows,
                "method": f"Monte Carlo ({ML.SIMULATIONS} runs): stage win rates (Beta posterior, "
                f"{ML.HALF_LIFE_MONTHS:.0f}-month half-life), slippage, run-rate bootstrap",
                "generated_at": env.now(),
            }
        )
    )
    run = audit_run(
        env,
        FORECAST,
        inputs={"team_id": team_id, "periods": periods, "deals": len(data["pipeline"])},
        output=[{k: r[k] for k in ("label", "p10", "p50", "p90")} for r in rows],
        text="\n".join(r["explanation"] for r in rows),
        model="monte_carlo",
        latency_ms=int((time.monotonic() - started) * 1000),
        company_id=company_id,
    )
    vals: dict[str, Any] = {"run_id": run.ids[0]}
    if narrative:
        vals["narrative"] = narrate_forecast(env, record, rows)
    record.write(vals)
    return record


def narrate_forecast(env: Environment, record: Model, rows: list[dict[str, Any]]) -> str | None:
    """Optional LLM narration of the computed figures; failures leave the forecast without it."""
    from tessera.ai import Source
    from tessera.ai.errors import AIError
    from tessera.ai.runtime import run_feature

    text = "\n".join(r["explanation"] for r in rows)
    try:
        result = run_feature(
            env, FORECAST, {}, sources=[Source(f"crm.ai_forecast:{record.ids[0]}", record.name, text)]
        )
    except (AIError, UserError) as exc:
        log.info("forecast narrative skipped", reason=str(exc))
        return None
    return result.text or None


@job("crm.ai_forecast", queue="ai", max_attempts=2, timeout_seconds=1800)
def forecast_job(env: Any, team_id: int | None = None, narrative: bool = False, periods: int = 3) -> dict[str, Any]:
    """Background forecast for the requesting user's company; notifies them with a link when done."""
    company_id = env.company.ids[0] if env.company else env.ref("core.main_company").ids[0]
    if not feature_enabled(env, FORECAST, company_id):
        return {"skipped": "feature_disabled"}
    record = compute_forecast(env, company_id, team_id, periods=periods, narrative=narrative)
    first = record.periods[0] if record.periods else {}
    _notify(
        env,
        [env.uid],
        "Sales forecast ready",
        f"{first.get('label', '')}: P50 {first.get('p50', 0):,.0f} (P10 {first.get('p10', 0):,.0f} – P90 "
        f"{first.get('p90', 0):,.0f})",
        url="/a/crm.action_reports",
        model="crm.ai_forecast",
        res_id=record.ids[0],
    )
    return {"forecast_id": record.ids[0]}


def forecast_dict(record: Model) -> dict[str, Any]:
    return {
        "id": record.ids[0],
        "name": record.name,
        "team_id": record.team_id.ids[0] if record.team_id else None,
        "periods": record.periods or [],
        "method": record.method,
        "narrative": record.narrative,
        "run_id": record.run_id or None,
        "generated_at": record.generated_at.isoformat() if record.generated_at else None,
        "ai_generated": True,
    }


# ----------------------------------------------------------------------------- lead panel


def score_dict(lead: Model) -> dict[str, Any]:
    rec = lead.elevated()
    model = rec.ai_score_model_id
    return {
        "score": rec.ai_score if rec.ai_scored_at else None,
        "probability": rec.ai_probability if rec.ai_scored_at else None,
        "factors": rec.ai_score_factors or [],
        "source": rec.ai_score_source,
        "scored_at": rec.ai_scored_at.isoformat() if rec.ai_scored_at else None,
        "model": {
            "version": model.version,
            "trained_at": model.trained_at.isoformat() if model.trained_at else None,
            "rows": (model.metrics or {}).get("rows"),
            "holdout_auc": (model.metrics or {}).get("holdout_auc"),
        }
        if model
        else None,
        "excluded_inputs": "gender, age, ethnicity, religion, disability, nationality and their proxies "
        "(names, location, language) are never model inputs",
    }


def _run_for_view(env: Environment, name: str, lead: Model, output: dict[str, Any], stamp: str | None) -> int | None:
    """Reuse the audit run of what this user was shown, or record a new one (feedback attaches to it)."""
    if not stamp or not ai_installed(env):
        return None
    runs = env["ai_core.run"].elevated()
    found = runs.search(
        [("feature", "=", name), ("res_model", "=", "crm.lead"), ("res_id", "=", lead.ids[0]), ("user_id", "=", env.uid)],
        order="id desc",
        limit=1,
    )
    if found and (found.input_ref or {}).get("inputs", {}).get("stamp") == stamp:
        return int(found.ids[0])
    return int(audit_run(env, name, record=lead, inputs={"stamp": stamp}, output=output, model=output.get("model_label", "")).ids[0])


def lead_insights(env: Environment, lead: Model) -> dict[str, Any]:
    """What the lead panel shows: stored score and risk of features enabled for the user's company."""
    lead.check_access("read")
    rec = lead.elevated()
    cid = rec.company_id.ids[0] if rec.company_id else None
    out: dict[str, Any] = {"lead_id": rec.ids[0], "ai_generated": True}
    if feature_enabled(env, SCORE, cid):
        score = score_dict(lead)
        model_label = f"{ML.ALGORITHM} v{score['model']['version']}" if score["model"] else "scoring rules"
        score["run_id"] = _run_for_view(env, SCORE, lead, {**score, "model_label": model_label}, score["scored_at"])
        out["score"] = score
    if feature_enabled(env, RISK, cid) and rec.type == "opportunity":
        risk = {
            "level": rec.ai_risk_level,
            "score": rec.ai_risk_score,
            "reasons": rec.ai_risk_reasons or [],
            "checked_at": rec.ai_risk_checked_at.isoformat() if rec.ai_risk_checked_at else None,
        }
        risk["run_id"] = _run_for_view(env, RISK, lead, {**risk, "model_label": "risk signals"}, risk["checked_at"])
        out["risk"] = risk
    return out


def rescore_now(env: Environment, lead: Model) -> dict[str, Any]:
    """Interactive refresh of one lead's score and risk (in-process, sub-second)."""
    lead.check_access("read")
    rec = lead.elevated()
    cid = rec.company_id.ids[0] if rec.company_id else None
    if feature_enabled(env, SCORE, cid):
        score_leads(lead)
    if feature_enabled(env, RISK, cid) and rec.type == "opportunity" and not rec.is_won and rec.active:
        risk = assess_deal(lead)
        _write_ai(
            lead,
            {
                "ai_risk_level": risk["level"],
                "ai_risk_score": risk["score"],
                "ai_risk_reasons": risk["reasons"],
                "ai_risk_checked_at": env.now(),
            },
        )
    return lead_insights(env, lead)
