"""CRM AI assist (Phase 1.6): lead extraction, follow-up drafts, call summaries, objection handling and
natural-language search.

Every feature is declared in ``ai/features/assist.yaml`` with its prompt in ``ai/prompts/assist_*.yaml``;
this module holds what they share — the retrieval opt-in of ``crm.lead``, the record sources (always built
as the requesting user, so access rights, record rules and field groups shape what a model sees), the
background jobs with their completion notifications and the small HTTP API of the lead form panels.
The features themselves live in ``ai_assist_*.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from tessera.ai.prompts import html_to_text
from tessera.ai.tools import Source
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import AccessError, MissingError, UserError
from tessera.orm import Model
from tessera.orm import fields as F

if TYPE_CHECKING:
    from tessera.orm.environment import Environment

LEAD = "crm.lead"
FEATURES = ("crm.lead_extract", "crm.followup_draft", "crm.call_summary", "crm.objections", "crm.nl_search")


class LeadRetrieval(Model):
    """Leads and their chatter are indexed for retrieval (objection handling cites past deals)."""

    _extend = "crm.lead"
    _ai_index = ["partner_id", "partner_name", "stage_id", "tag_ids", "lost_reason_id", "lost_feedback", "description"]


# ---------------------------------------------------------------------------------------------- helpers


def ai_ready(env: Environment) -> bool:
    return "ai_core.feature" in env.registry.models


def feature_enabled(env: Environment, name: str, company_id: int | None) -> bool:
    if not ai_ready(env):
        return False
    feature = env["ai_core.feature"].elevated().search([("name", "=", name)], limit=1)
    return bool(feature) and bool(feature.enabled_for(company_id))


def readable_lead(env: Environment, record: Any) -> Model:
    """The lead a feature works on, checked readable by the requesting user."""
    if record is None or getattr(record, "_name", None) != LEAD or not record.ids:
        raise UserError("Open a lead or opportunity first")
    lead = env[LEAD].with_context(active_test=False).browse(record.ids[:1])
    if not lead.exists():
        raise MissingError("This lead no longer exists")
    lead.check_access("read")
    return lead


def _value(rec: Model, name: str) -> Any:
    """A field value as the user sees it (``None`` for fields or linked records they may not read)."""
    field = rec._fields.get(name)
    if field is None or not rec._check_field_access(field, "read"):
        return None
    try:
        value = rec[name]
        if isinstance(field, F.Many2one):
            return value.display_name if value else None
        if isinstance(field, F.Many2many | F.One2many):
            return ", ".join(value.mapped("display_name")) or None
        if isinstance(field, F.Html):
            return html_to_text(value or "").strip() or None
        if isinstance(field, F.Selection):
            return dict(field.selection).get(value, value) if value else None
        return None if value is None or value is False or value == "" else value
    except AccessError:
        return None


def money(amount: float | None, currency: str | None = None) -> str:
    """``18,900.00 USD`` — one unambiguous format for every amount placed in a prompt."""
    text = f"{float(amount or 0):,.2f}"
    return f"{text} {currency}" if currency else text


def lead_text(lead: Model) -> str:
    """The lead as plain text, read as the requesting user."""
    currency = _value(lead, "currency_id")
    revenue = _value(lead, "expected_revenue")
    rows = [
        ("Opportunity", _value(lead, "name")),
        ("Type", _value(lead, "type")),
        ("Stage", _value(lead, "stage_id")),
        ("Customer", _value(lead, "partner_id")),
        ("Company name", _value(lead, "partner_name")),
        ("Contact", _value(lead, "contact_name")),
        ("Job position", _value(lead, "function")),
        ("Email", _value(lead, "email_from")),
        ("Phone", _value(lead, "phone")),
        ("City", _value(lead, "city")),
        ("Country", _value(lead, "country_id")),
        ("Salesperson", _value(lead, "user_id")),
        ("Sales team", _value(lead, "team_id")),
        ("Expected revenue (internal estimate, do not quote)", revenue and money(revenue, currency)),
        ("Probability", f"{lead.probability:.0f}%" if _value(lead, "probability") is not None else None),
        ("Expected closing", _value(lead, "date_deadline")),
        ("Priority", _value(lead, "priority")),
        ("Tags", _value(lead, "tag_ids")),
        ("Lost reason", _value(lead, "lost_reason_id")),
        ("Lost feedback", _value(lead, "lost_feedback")),
        ("Notes", _value(lead, "description")),
    ]
    return "\n".join(f"{k}: {v}" for k, v in rows if v not in (None, ""))


def thread_messages(env: Environment, model: str, res_id: int, limit: int = 15) -> list[dict[str, Any]]:
    """Latest discussion of a record the user may read (oldest first)."""
    from modules.chatter.thread import check_thread_access

    check_thread_access(env, model, res_id, "read")
    msgs = (
        env["chatter.message"]
        .elevated()
        .search(
            [("model", "=", model), ("res_id", "=", res_id), ("message_type", "in", ["comment", "note", "email"])],
            order="id desc",
            limit=limit,
        )
    )
    out = []
    for m in reversed(list(msgs)):
        out.append(
            {
                "id": m.ids[0],
                "author": m.author_id.display_name if m.author_id else (m.email_from or "Unknown"),
                "date": str(m.create_date)[:16] if m.create_date else "",
                "kind": "internal note" if m.message_type == "note" else m.message_type,
                "subject": m.subject or "",
                "text": html_to_text(m.body or "").strip()[:2000],
            }
        )
    return out


def thread_text(messages: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"[{m['date']}] {m['author']} ({m['kind']}){': ' + m['subject'] if m['subject'] else ''}\n{m['text']}"
        for m in messages
    )


def lead_source(env: Environment, lead: Model, *, thread: int = 0) -> Source:
    """The lead (and, with ``thread``, its latest messages) as one source citable as ``crm.lead:<id>``."""
    text = lead_text(lead)
    if thread:
        msgs = thread_messages(env, LEAD, lead.ids[0], thread)
        if msgs:
            text += "\n\nDiscussion (oldest first):\n" + thread_text(msgs)
    return Source(f"{LEAD}:{lead.ids[0]}", lead.display_name or "Lead", text)


def notify(env: Environment, lead: Model, title: str, body: str) -> None:
    """Background features end with an in-app notification linking to the lead."""
    env["core.notification"].notify(
        [env.uid],
        title,
        body,
        kind="system",
        model=LEAD,
        res_id=lead.ids[0],
        url=f"/m/{LEAD}/{lead.ids[0]}",
    )


# ---------------------------------------------------------------------------------------------- HTTP API

router = register_router(APIRouter(prefix="/crm/ai", tags=["crm", "ai"]))


class CallNotesIn(BaseModel):
    notes: str = Field(min_length=10, max_length=20_000)


@router.post("/leads/{lead_id}/call-summary", status_code=202)
def queue_call_summary(lead_id: int, data: CallNotesIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Summarise pasted call notes in the background; the requester is notified with a link when done."""
    from modules.crm.ai_assist_calls import queue

    return queue(ctx.env, lead_id, data.notes)


@router.get("/jobs/{job_id}")
def assist_job(job_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """State of one of my CRM assist jobs and, once done, the AI run it produced."""
    job = ctx.env["core.job"].elevated().browse([job_id]).exists()
    if not job or not str(job.name).startswith("crm.ai_") or job.user_id.ids[:1] != [ctx.env.uid]:
        raise MissingError("No such job")
    result = job.result or {}
    return {
        "id": job.ids[0],
        "state": job.state,
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "message": result.get("message"),
    }


# feature modules register their handlers, jobs, hooks and named actions on import
from modules.crm import ai_assist_calls as _calls  # noqa: E402, F401
from modules.crm import ai_assist_draft as _draft  # noqa: E402, F401
from modules.crm import ai_assist_extract as _extract  # noqa: E402, F401
from modules.crm import ai_assist_objections as _objections  # noqa: E402, F401
from modules.crm import ai_assist_search as _search  # noqa: E402, F401
