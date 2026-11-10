"""``crm.lead_extract`` — lead fields from an inbound e-mail or a web-form payload.

Every intake channel (the ``crm@`` alias, the web form and the intake API) already creates the lead as a
draft (``type = lead``) and logs a ``crm.lead_intake`` row. Creating that row queues a background job
(after commit, so the intake itself never waits for a model) that runs the extraction **as the person
who will review it** — the lead's salesperson, else the team leader — and turns the extracted values
into one write proposal holding only the fields that would change. Nothing is filled in until that
person accepts it; they get a notification linking to the lead.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import re
from typing import TYPE_CHECKING, Any

from modules.crm.ai_assist import LEAD, ai_ready, feature_enabled, notify, readable_lead, thread_messages
from tessera.ai import Source
from tessera.ai.errors import AIError, AIUnavailable
from tessera.ai.prompts import html_to_text
from tessera.ai.runtime import handler, run_feature
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import AccessError
from tessera.jobs import enqueue, job
from tessera.orm.hooks import hook

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "crm.lead_extract"
FIELD_CLASSES: dict[str, list[str]] = {
    "contact": ["contact_name", "email_from", "phone", "mobile", "function"],
    "company": ["partner_name", "website", "street", "city", "zip", "country_code"],
    "deal": ["expected_revenue", "date_deadline"],
}
CHANNELS = ("email", "webform", "api")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def intake_text(env: Environment, lead: Model) -> tuple[str, str]:
    """(channel, raw text) of the lead's latest intake, read as the requesting user."""
    intake = env["crm.lead_intake"].search([("lead_id", "=", lead.ids[0])], limit=1)
    channel = intake.channel if intake else "manual"
    payload = (intake.payload or {}) if intake else {}
    if channel == "email":
        emails = [m for m in thread_messages(env, LEAD, lead.ids[0], 30) if m["kind"] == "email"]
        if emails:
            first = emails[0]
            return channel, f"From: {first['author']}\nSubject: {first['subject']}\n\n{first['text']}"
        sender = payload.get("from") or lead.email_from or ""
        subject = payload.get("subject") or lead.name
        return channel, f"From: {sender}\nSubject: {subject}\n\n{html_to_text(lead.description or '').strip()}"
    rows = [
        ("Name", lead.contact_name),
        ("Email", lead.email_from),
        ("Phone", lead.phone),
        ("Company", lead.partner_name),
        ("Subject", lead.name),
        ("Page", intake.source_ref if intake else None),
    ]
    head = "\n".join(f"{k}: {v}" for k, v in rows if v)
    return channel, f"{head}\nMessage:\n{html_to_text(lead.description or '').strip()}"


def _country(env: Environment, code: str | None) -> int | None:
    if not code or not isinstance(code, str) or len(code.strip()) != 2:
        return None
    found = env["core.country"].search([("code", "=", code.strip().upper())], limit=1)
    return found.ids[0] if found else None


def lead_values(env: Environment, fields: dict[str, Any]) -> dict[str, Any]:
    """Extracted fields → ``crm.lead`` values; anything malformed is dropped rather than proposed."""
    out: dict[str, Any] = {}
    for name in [*FIELD_CLASSES["contact"], *FIELD_CLASSES["company"]]:
        value = fields.get(name)
        if name == "country_code":
            country = _country(env, value)
            if country:
                out["country_id"] = country
            continue
        if isinstance(value, str) and value.strip():
            value = value.strip()[:200]
            if name == "email_from" and not EMAIL.match(value):
                continue
            out[name] = value.lower() if name == "email_from" else value
    revenue = fields.get("expected_revenue")
    if isinstance(revenue, int | float) and not isinstance(revenue, bool) and 0 < revenue < 1e12:
        out["expected_revenue"] = float(revenue)
    deadline = fields.get("date_deadline")
    if isinstance(deadline, str):
        with contextlib.suppress(ValueError):
            out["date_deadline"] = dt.date.fromisoformat(deadline).isoformat()
    return out


def _differs(current: Any, proposed: Any) -> bool:
    if hasattr(current, "_ids"):
        return bool((current.ids[0] if current.ids else None) != proposed)
    if isinstance(current, dt.date):
        return bool(current.isoformat() != proposed)
    if isinstance(current, float | int) and isinstance(proposed, float | int):
        return abs(float(current) - float(proposed)) > 0.005
    return bool((current or "") != proposed)


def changes_for(lead: Model, values: dict[str, Any]) -> dict[str, Any]:
    """Only the fields that would change and that the user may write."""
    try:
        lead.check_access("write")
    except AccessError:
        return {}
    return {
        k: v
        for k, v in values.items()
        if k in lead._fields and lead._check_field_access(lead._fields[k], "write") and _differs(lead[k], v)
    }


@handler(FEATURE)
def prepare(env: Environment, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    lead = readable_lead(env, record)
    channel, text = intake_text(env, lead)
    if inputs.get("text"):
        channel, text = "pasted text", str(inputs["text"])
    source = Source(f"{LEAD}:{lead.ids[0]}", f"Inbound {channel}", text)

    def on_output(ctx: ToolContext, _text: str, data: Any) -> None:
        fields = (data or {}).get("fields") if isinstance(data, dict) else None
        changes = changes_for(lead, lead_values(env, fields or {}))
        if changes:
            ctx.proposals.append(
                PendingProposal(
                    "write", LEAD, lead.ids[0], changes, f"Fill {len(changes)} lead fields from the {channel}"
                )
            )

    return {"sources": [source], "on_output": on_output}


# ---------------------------------------------------------------------------------------------- background


def reviewer(lead: Model) -> int | None:
    """Who reviews the extraction: the salesperson, else the team leader (internal, active users only)."""
    for user in (lead.user_id, lead.team_id.user_id if lead.team_id else lead.user_id.browse([])):
        if user and user.active and not user.share:
            return int(user.ids[0])
    return None


@hook("after_create")
def extract_on_intake(records: Any, vals_list: list[dict[str, Any]]) -> None:
    """Every inbound lead (e-mail alias, web form, intake API) queues an extraction job after commit."""
    if records._name != "crm.lead_intake" or not ai_ready(records.env):
        return
    env = records.env
    for intake in records.elevated():
        company = intake.lead_id.company_id.ids[0] if intake.lead_id.company_id else None
        if intake.channel in CHANNELS and feature_enabled(env, FEATURE, company):
            enqueue(
                env,
                "crm.ai_lead_extract",
                {"intake_id": intake.ids[0]},
                idempotency_key=f"{FEATURE}:{intake.ids[0]}",
                description=f"AI lead extraction for {intake.lead_id.name}",
            )


@job("crm.ai_lead_extract", queue="ai", max_attempts=3, backoff_seconds=120, as_system=True)
def lead_extract_job(env: Any, intake_id: int) -> dict[str, Any]:
    intake = env["crm.lead_intake"].browse([intake_id]).exists()
    if not intake or not intake.lead_id:
        return {"status": "skipped", "message": "intake no longer exists"}
    lead = intake.lead_id
    uid = reviewer(lead)
    if uid is None:
        return {"status": "skipped", "message": "no salesperson or team leader to review the extraction"}
    company = lead.company_id.ids[0] if lead.company_id else None
    user_env = env.with_user(uid)
    if company:
        user_env = user_env.with_company(company)
    user_lead = user_env[LEAD].browse(lead.ids)
    try:
        result = run_feature(user_env, FEATURE, {}, record=user_lead, **prepare(user_env, user_lead, {}))
    except AIUnavailable:
        raise  # retried with backoff
    except AIError as exc:
        return {"status": exc.code, "run_id": exc.context.get("run_id"), "message": exc.message}
    fields = sum(len(p.payload) for p in result.proposals)
    notify(
        user_env,
        user_lead,
        f"AI read the {intake.channel} for {lead.name}",
        f"{fields} field suggestions to review" if fields else "Nothing new to fill in",
    )
    return {"status": "ok", "run_id": result.run.ids[0], "proposals": len(result.proposals)}


# ---------------------------------------------------------------------------------------------- evaluation


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """Micro-averaged F1 per field class (contact, company, deal) over the whole set."""
    out: dict[str, float] = {}
    for cls, names in FIELD_CLASSES.items():
        tp = fp = fn = 0
        for r in results:
            expected = (r["_case"].get("expected") or {}).get("fields")
            if expected is None:
                continue
            got = ((r.get("data") or {}).get("fields") or {}) if isinstance(r.get("data"), dict) else {}
            for name in names:
                e, g = _norm(expected.get(name)), _norm(got.get(name))
                if g is not None and g == e:
                    tp += 1
                else:
                    fp += g is not None
                    fn += e is not None
        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        out[f"field_f1_{cls}"] = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return out


def _norm(value: Any) -> Any:
    if value in (None, "", [], {}):
        return None
    return value.strip().lower() if isinstance(value, str) else value
