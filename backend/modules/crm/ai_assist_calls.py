"""``crm.call_summary`` — call and meeting notes → summary + proposed field values (background).

The user pastes a transcript or notes on the lead form; a job (run as that user, in their company)
summarises them and proposes, separately so each can be accepted or rejected on its own:

* the summary as an internal note on the lead (message proposal, ``internal``);
* one write proposal per lead field the notes give a new value for (stage, revenue, probability,
  closing date, priority, contact details);
* the agreed next step as a to-do (action proposal ``chatter.schedule_activity``).

Nothing changes until the user accepts; the run, its proposals and decisions form the audit trail
shown in the panel, and a notification links back to the lead when the job is done.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING, Any

from modules.crm.ai_assist import LEAD, lead_source, notify, readable_lead
from tessera.ai import Source
from tessera.ai.errors import AIError
from tessera.ai.runtime import check_enabled, get_feature, handler, run_feature
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import AccessError
from tessera.jobs import enqueue, job

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "crm.call_summary"
FIELDS = ["stage", "expected_revenue", "probability", "date_deadline", "priority", "contact_name", "function", "phone"]
LABELS = {
    "stage_id": "Stage",
    "expected_revenue": "Expected revenue",
    "probability": "Probability",
    "date_deadline": "Expected closing",
    "priority": "Priority",
    "contact_name": "Contact",
    "function": "Job position",
    "phone": "Phone",
}


def _date(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _number(value: Any, low: float, high: float) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and low <= value <= high:
        return float(value)
    return None


def field_values(env: Environment, fields: dict[str, Any]) -> dict[str, tuple[Any, str]]:
    """Model answer → ``{lead field: (value, shown as)}``; unknown stages and malformed values are dropped."""
    out: dict[str, tuple[Any, str]] = {}
    stage = fields.get("stage")
    if isinstance(stage, str) and stage.strip():
        found = env["crm.stage"].search([("name", "=ilike", stage.strip())], limit=1)
        if found:
            out["stage_id"] = (found.ids[0], found.name)
    if (revenue := _number(fields.get("expected_revenue"), 0.01, 1e12)) is not None:
        out["expected_revenue"] = (revenue, f"{revenue:,.2f}")
    if (probability := _number(fields.get("probability"), 0, 100)) is not None:
        out["probability"] = (probability, f"{probability:g}%")
    if deadline := _date(fields.get("date_deadline")):
        out["date_deadline"] = (deadline, deadline)
    if fields.get("priority") in ("0", "1", "2", "3"):
        out["priority"] = (fields["priority"], ["Normal", "Good", "Very good", "Excellent"][int(fields["priority"])])
    for name in ("contact_name", "function", "phone"):
        value = fields.get(name)
        if isinstance(value, str) and value.strip():
            out[name] = (value.strip()[:200], value.strip()[:200])
    return out


def _current(lead: Model, name: str) -> Any:
    value = lead[name]
    if hasattr(value, "_ids"):
        return value.ids[0] if value.ids else None
    if isinstance(value, dt.date):
        return value.isoformat()
    return value or None


def note_body(data: dict[str, Any]) -> str:
    parts = [str(data.get("summary") or "").strip()]
    points = [p for p in data.get("key_points") or [] if isinstance(p, str) and p.strip()]
    if points:
        parts.append("Key points:\n" + "\n".join(f"- {p.strip()}" for p in points))
    step = data.get("next_step") or {}
    if isinstance(step, dict) and step.get("summary"):
        when = f" (by {step['due_date']})" if step.get("due_date") else ""
        parts.append(f"Next step: {step['summary']}{when}")
    return "Call summary\n\n" + "\n\n".join(p for p in parts if p)


@handler(FEATURE)
def prepare(env: Environment, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    lead = readable_lead(env, record)
    stages = env["crm.stage"].search([], order="sequence, id")
    pipeline = Source(
        f"crm.stage:{stages.ids[0]}" if stages else f"{LEAD}:{lead.ids[0]}",
        "Pipeline stages",
        "\n".join(f"- {s.name} (probability {s.probability:g}%)" for s in stages),
    )
    notes = Source(f"{LEAD}:{lead.ids[0]}", f"Call notes on {lead.display_name}", str(inputs.get("notes") or ""))
    context = lead_source(env, lead)

    def on_output(ctx: ToolContext, _text: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        ctx.proposals.append(
            PendingProposal(
                "message",
                LEAD,
                lead.ids[0],
                {"body": note_body(data), "internal": True},
                "Log the call summary as an internal note",
            )
        )
        try:
            lead.check_access("write")
            writable = True
        except AccessError:
            writable = False
        if writable:
            for name, (value, shown) in field_values(env, data.get("fields") or {}).items():
                if not lead._check_field_access(lead._fields[name], "write") or _current(lead, name) == value:
                    continue
                ctx.proposals.append(
                    PendingProposal("write", LEAD, lead.ids[0], {name: value}, f"{LABELS[name]} → {shown}")
                )
        step = data.get("next_step")
        if isinstance(step, dict) and str(step.get("summary") or "").strip():
            due = _date(step.get("due_date"))
            ctx.proposals.append(
                PendingProposal(
                    "action",
                    LEAD,
                    lead.ids[0],
                    {
                        "action": "chatter.schedule_activity",
                        "args": {"summary": str(step["summary"]).strip()[:200], "date_deadline": due},
                    },
                    f"To-do: {str(step['summary']).strip()[:120]}" + (f" by {due}" if due else ""),
                )
            )

    # the lead (current values) and the notes are separate envelopes; the notes are what is summarised
    return {"sources": [context, notes, pipeline], "on_output": on_output}


# ---------------------------------------------------------------------------------------------- background


def queue(env: Environment, lead_id: int, notes: str) -> dict[str, Any]:
    """Check access and the kill switch now (404 when disabled), then summarise in the background."""
    lead = readable_lead(env, env[LEAD].browse([lead_id]))
    check_enabled(env, get_feature(env, FEATURE))
    queued = enqueue(
        env,
        "crm.ai_call_summary",
        {"lead_id": lead.ids[0], "notes": notes},
        description=f"AI call summary for {lead.display_name}",
    )
    return {"job_id": queued.ids[0], "state": queued.state}


@job("crm.ai_call_summary", queue="ai", max_attempts=1, timeout_seconds=600)
def call_summary_job(env: Any, lead_id: int, notes: str) -> dict[str, Any]:
    """Runs as the user who pasted the notes (jobs run as their enqueuer), in their company."""
    lead = env[LEAD].browse([lead_id])
    inputs = {"notes": notes}
    try:
        result = run_feature(env, FEATURE, inputs, record=lead, **prepare(env, lead, inputs))
    except AIError as exc:
        notify(env, lead, f"Call summary failed for {lead.display_name}", exc.message[:200])
        return {"status": exc.code, "run_id": exc.context.get("run_id"), "message": exc.message}
    changes = sum(1 for p in result.proposals if p.kind == "write")
    notify(
        env,
        lead,
        f"Call summary ready for {lead.display_name}",
        f"Summary and {changes} field suggestion{'s' if changes != 1 else ''} to review",
    )
    return {"status": "ok", "run_id": result.run.ids[0], "proposals": len(result.proposals)}
