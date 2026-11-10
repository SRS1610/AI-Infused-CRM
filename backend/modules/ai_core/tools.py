"""Built-in tools available to features that whitelist them. All run as the requesting user."""

from __future__ import annotations

from typing import Any

from tessera.ai import retrieval
from tessera.ai.prompts import html_to_text
from tessera.ai.tools import ToolContext, tool
from tessera.exceptions import AccessError, MissingError, ValidationError
from tessera.i18n import _
from tessera.orm import fields as F

MAX_FIELDS = 30


def _model(ctx: ToolContext, model: str) -> Any:
    if model not in ctx.env.registry.models or not ctx.env.registry.models[model]._auto:
        raise ValidationError(_("Unknown model '{model}'", model=model))
    return ctx.env[model]


def _plain(rec: Any, names: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": rec.ids[0], "display_name": rec.display_name}
    for name in names:
        f = rec._fields[name]
        if isinstance(f, F.Binary | F.Vector):
            continue
        try:
            value = rec[name]
            if isinstance(f, F.Many2one):
                out[name] = value.display_name if value else None
            elif isinstance(f, F.One2many | F.Many2many):
                out[name] = value.mapped("display_name")[:20]
            elif isinstance(f, F.Html):
                out[name] = html_to_text(value or "")[:4000]
            else:
                out[name] = value if not hasattr(value, "isoformat") else value.isoformat()
        except AccessError:
            out[name] = "(not visible to you)"  # a linked record the user cannot read
    return out


@tool("records.read", "Read fields of one record the user can access.")
def read_record(ctx: ToolContext, model: str, record_id: int, fields: list[str] | None = None) -> dict[str, Any]:
    rec = _model(ctx, model).browse([record_id])
    if not rec.exists():
        raise MissingError(_("No such record"))
    rec.check_access("read")
    readable = [
        f.name
        for f in rec._readable_fields()
        if f.name != "id"
        and not isinstance(f, F.Binary | F.Vector)
        and (not f.comodel_name or ctx.env[f.comodel_name].check_access_rights("read", raise_exception=False))
    ]
    default = [n for n in readable if not isinstance(rec._fields[n], F.One2many)]
    names = [n for n in (fields or default) if n in readable][:MAX_FIELDS]
    data = _plain(rec, names)
    ctx.cite(rec, "\n".join(f"{k}: {v}" for k, v in data.items()))
    return {"source": f"{model}:{record_id}", **data}


@tool("records.search", "Find records of a model by name; returns ids and names the user can see.")
def search_records(ctx: ToolContext, model: str, query: str, limit: int = 5) -> list[dict[str, Any]]:
    m = _model(ctx, model)
    rec_name = type(m)._rec_name if type(m)._rec_name in m._fields else "display_name"
    found = m.search([(rec_name, "ilike", query)], limit=min(limit, 20))
    return [{"id": r.ids[0], "name": r.display_name, "source": f"{model}:{r.ids[0]}"} for r in found]


@tool("chatter.read_thread", "Read the latest messages of a record's discussion thread.")
def read_thread(ctx: ToolContext, model: str, record_id: int, limit: int = 15) -> list[dict[str, Any]]:
    from modules.chatter.thread import check_thread_access

    rec = check_thread_access(ctx.env, model, record_id, "read")
    msgs = (
        ctx.env["chatter.message"]
        .elevated()
        .search(
            [("model", "=", model), ("res_id", "=", record_id), ("message_type", "in", ["comment", "note", "email"])],
            order="id desc",
            limit=min(limit, 50),
        )
    )
    out = []
    for m in reversed(list(msgs)):
        text = html_to_text(m.body or "").strip()
        out.append(
            {
                "author": m.author_id.display_name if m.author_id else m.email_from,
                "date": str(m.create_date),
                "text": text[:2000],
            }
        )
    ctx.cite(rec, "\n".join(f"{o['author']}: {o['text']}" for o in out))
    return out


@tool("retrieval.search", "Search company records and discussions the user can access; returns cited passages.")
def retrieval_search(ctx: ToolContext, query: str, limit: int = 6) -> list[dict[str, Any]]:
    hits = retrieval.search(ctx.env, query, limit=min(limit, 12))
    for h in hits:
        ctx.sources[h.source.id] = h.source
    return [{"source": h.source.id, "title": h.source.title, "text": h.source.text[:1500]} for h in hits]


@tool("records.propose_write", "Propose changing field values of a record (a person reviews it).", writes=True)
def propose_write(ctx: ToolContext, model: str, record_id: int, values: dict[str, Any]) -> dict[str, Any]:
    rec = _model(ctx, model).browse([record_id])
    if not rec.exists():
        raise MissingError(_("No such record"))
    rec.check_access("write")  # proposing a change the user could not make is pointless
    unknown = [k for k in values if k not in rec._fields or not rec._check_field_access(rec._fields[k], "write")]
    if unknown:
        raise AccessError(_("Fields not writable: {unknown}", unknown=", ".join(unknown)))
    return ctx.propose("write", model, record_id, payload=values, title=f"Update {rec.display_name}")


@tool("records.propose_create", "Propose creating a record (a person reviews it).", writes=True)
def propose_create(ctx: ToolContext, model: str, values: dict[str, Any]) -> dict[str, Any]:
    m = _model(ctx, model)
    m.check_access_rights("create")
    return ctx.propose("create", model, None, payload=values, title=f"New {m._description}")


@tool(
    "chatter.propose_message",
    "Propose posting a message on a record's thread (a person reviews and sends it).",
    writes=True,
)
def propose_message(ctx: ToolContext, model: str, record_id: int, body: str, subject: str = "") -> dict[str, Any]:
    from modules.chatter.thread import check_thread_access

    rec = check_thread_access(ctx.env, model, record_id, "read")
    return ctx.propose(
        "message", model, record_id, payload={"body": body, "subject": subject}, title=f"Message on {rec.display_name}"
    )


@tool("chatter.propose_activity", "Propose scheduling a to-do on a record (a person reviews it).", writes=True)
def propose_activity(ctx: ToolContext, model: str, record_id: int, summary: str, due_date: str = "") -> dict[str, Any]:
    rec = _model(ctx, model).browse([record_id])
    rec.check_access("read")
    return ctx.propose(
        "action",
        model,
        record_id,
        payload={
            "action": "chatter.schedule_activity",
            "args": {"summary": summary, "date_deadline": due_date or None},
        },
        title=f"To-do on {rec.display_name}: {summary}",
    )
