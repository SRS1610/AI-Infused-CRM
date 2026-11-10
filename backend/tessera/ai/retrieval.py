"""Retrieval-augmented generation over company data.

Indexing: models opt in with ``_ai_index = ["name", "description", ...]``; record text is chunked
(~400 tokens with overlap), unchanged chunks are skipped by content hash, and vectors come from the
company's embedding route through the gateway. Chatter messages are indexed under their parent
record, so their visibility follows the record (D-036). Archived records leave the index unless they
match the model's ``_ai_index_archived`` domain (lost opportunities stay: objection handling learns from
them); they are then found only by searches that include archived records (``active_test=False``).
A model can prefix its indexed text with ``_ai_index_header()`` (e.g. the outcome of a deal).

Searching filters by access **before** ranking: for every model in scope the user's own accessible
id query (the ORM's record-rule compiler) is joined into the vector query, together with the
company filter, so a chunk the user cannot read is never ranked and can never reach a prompt or a
citation (1.5-h). Vector and trigram rankings are fused with reciprocal rank fusion.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, literal, or_, select

from tessera.ai.client import CallContext, get_client
from tessera.ai.errors import AIError
from tessera.ai.prompts import html_to_text
from tessera.ai.tools import Source
from tessera.logging import get_logger
from tessera.orm import fields as F

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)
CHUNK_CHARS = 1600
OVERLAP = 200
RRF_K = 60
INDEX_FEATURE = "ai_core.retrieval_index"


def indexed_fields(model: Model | type[Model]) -> list[str]:
    return list(getattr(model if isinstance(model, type) else type(model), "_ai_index", []) or [])


def chunk(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP) -> list[str]:
    text = " ".join((text or "").split())
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            space = text.rfind(" ", start + size // 2, end)
            end = space if space > start else end
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [c for c in out if c]


def keeps_archived(rec: Model) -> bool:
    """Whether the archived record ``rec`` stays in the index (its model's ``_ai_index_archived`` domain)."""
    from tessera.orm.domain import evaluate, parse

    domain = getattr(type(rec), "_ai_index_archived", None)
    return bool(domain) and evaluate(parse(domain), rec)


def record_text(rec: Model) -> str:
    parts = [rec.display_name or ""]
    header = getattr(rec, "_ai_index_header", None)
    if callable(header) and (line := header()):
        parts.append(str(line))
    for name in indexed_fields(rec):
        f = rec._fields.get(name)
        if f is None:
            continue
        value = rec[name]
        if isinstance(f, F.Many2one | F.Many2many | F.One2many):
            value = ", ".join(value.mapped("display_name"))
        elif isinstance(f, F.Html):
            value = html_to_text(value or "")
        if value not in (None, False, ""):
            parts.append(f"{f.label}: {value}")
    return "\n".join(parts)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _embed(
    env: Environment, company_id: int, texts: list[str], input_type: str = "document"
) -> tuple[list[list[float]], str]:
    client = get_client(env)
    resp = client.embed(CallContext(company_id, env.uid, INDEX_FEATURE), texts, input_type)
    return resp["vectors"], resp.get("model", "")


def _store_chunks(env: Environment, items: list[dict[str, Any]]) -> int:
    """``items``: dicts with company_id, res_model, res_id, source_kind, source_id, title, text."""
    emb = env["ai_core.embedding"].elevated("ai index")
    written = 0
    by_company: dict[int, list[dict[str, Any]]] = {}
    for item in items:
        key = (item["res_model"], item["res_id"], item["source_kind"], item.get("source_id") or 0)
        existing = emb.search(
            [
                ("res_model", "=", key[0]),
                ("res_id", "=", key[1]),
                ("source_kind", "=", key[2]),
                ("source_id", "=", key[3]),
            ]
        )
        chunks = chunk(item["text"])
        hashes = [_hash(c) for c in chunks]
        if sorted(existing.mapped("content_hash")) == sorted(hashes) and set(
            existing.mapped("company_id").ids or [None]
        ) == {item["company_id"]}:
            continue
        existing.unlink()
        for i, (c, h) in enumerate(zip(chunks, hashes, strict=True)):
            by_company.setdefault(item["company_id"] or env.company.ids[0], []).append(
                {**item, "chunk_index": i, "content": c, "content_hash": h}
            )
    for company_id, rows in by_company.items():
        vectors, model = _embed(env, company_id, [r["content"] for r in rows])
        emb.create(
            [
                {
                    "company_id": r["company_id"],
                    "res_model": r["res_model"],
                    "res_id": r["res_id"],
                    "source_kind": r["source_kind"],
                    "source_id": r.get("source_id") or 0,
                    "chunk_index": r["chunk_index"],
                    "title": (r.get("title") or "")[:250],
                    "content": r["content"],
                    "content_hash": r["content_hash"],
                    "vector": v,
                    "embed_model": model,
                }
                for r, v in zip(rows, vectors, strict=True)
            ]
        )
        written += len(rows)
    return written


def index_records(records: Model) -> int:
    if not indexed_fields(records) or not records._ids:
        return 0
    items = []
    for rec in records.elevated("ai index").with_context(active_test=False):
        if "active" in rec._fields and not rec.active and not keeps_archived(rec):
            remove(rec.env, rec._name, rec.ids)
            continue
        company = rec.company_id.ids[0] if "company_id" in rec._fields and rec.company_id else None
        items.append(
            {
                "company_id": company,
                "res_model": rec._name,
                "res_id": rec.ids[0],
                "source_kind": "record",
                "source_id": 0,
                "title": rec.display_name or "",
                "text": record_text(rec),
            }
        )
    return _store_chunks(records.env, items)


def index_messages(env: Environment, messages: Model) -> int:
    items = []
    for msg in messages.elevated("ai index"):
        cls = env.registry.models.get(msg.model)
        if cls is None or not indexed_fields(cls) or msg.message_type in ("notification", "system"):
            continue
        parent = env[msg.model].elevated().with_context(active_test=False).browse([msg.res_id]).exists()
        if not parent or ("active" in parent._fields and not parent.active and not keeps_archived(parent)):
            continue
        company = parent.company_id.ids[0] if "company_id" in parent._fields and parent.company_id else None
        text = html_to_text(msg.body or "")
        if msg.subject:
            text = f"{msg.subject}\n{text}"
        items.append(
            {
                "company_id": company,
                "res_model": msg.model,
                "res_id": msg.res_id,
                "source_kind": "message",
                "source_id": msg.ids[0],
                "title": f"{parent.display_name} — message",
                "text": text,
            }
        )
    return _store_chunks(env, items)


def remove(env: Environment, model: str, ids: list[int]) -> None:
    if ids and "ai_core.embedding" in env.registry.models:
        env.cr.execute(
            "DELETE FROM ai_core_embedding WHERE res_model = :m AND res_id = ANY(:ids)", {"m": model, "ids": list(ids)}
        )


@dataclass
class Hit:
    source: Source
    res_model: str
    res_id: int
    score: float
    distance: float | None


def _accessible_ids(env: Environment, model: str) -> Any | None:
    """The SQL selecting the ids ``env``'s user may read in ``model`` (None: no read access at all)."""
    from tessera.orm.sql import Query

    rec = env[model]
    if not rec.check_access_rights("read", raise_exception=False):
        return None
    return Query(rec).select_ids(rec._search_domain([]), None, None, 0)


def search(
    env: Environment, query: str, *, models: list[str] | None = None, limit: int = 8, internal: bool = True
) -> list[Hit]:
    """Ranked chunks the user may read. ``models`` restricts the scope (default: every indexed model)."""
    if not query.strip() or "ai_core.embedding" not in env.registry.models:
        return []
    scope = [m for m, cls in env.registry.models.items() if indexed_fields(cls) and (models is None or m in models)]
    emb = env.registry.table_for("ai_core.embedding")
    allowed = []
    for m in scope:
        ids = _accessible_ids(env, m)
        if ids is not None:
            allowed.append(
                and_(
                    emb.c.res_model == m,
                    emb.c.res_id.in_(ids.scalar_subquery() if hasattr(ids, "scalar_subquery") else ids),
                )
            )
    if not allowed:
        return []
    companies = env.companies.ids
    visible = and_(or_(*allowed), or_(emb.c.company_id.is_(None), emb.c.company_id.in_(companies)))
    if not internal:
        visible = and_(visible, emb.c.source_kind != "message")
    company = env.company.ids[0] if env.company else companies[0]
    try:
        vectors, _ = _embed(env, company, [query], "query")
        qvec = vectors[0]
    except AIError as exc:
        log.warning("query embedding failed, lexical retrieval only", error=str(exc))
        qvec = None
    fetch = max(limit * 3, 20)
    ranked: dict[int, dict[str, Any]] = {}
    cols = [emb.c.id, emb.c.res_model, emb.c.res_id, emb.c.title, emb.c.content]
    if qvec is not None:
        dist = emb.c.vector.cosine_distance(qvec)
        rows = env.cr.execute(select(*cols, dist.label("d")).where(visible).order_by(dist).limit(fetch)).all()
        for rank, r in enumerate(rows):
            ranked.setdefault(r.id, {"row": r, "score": 0.0, "d": float(r.d)})["score"] += 1 / (RRF_K + rank + 1)
    sim = func.similarity(emb.c.content, literal(query))
    rows = env.cr.execute(
        select(*cols, sim.label("s")).where(and_(visible, sim > 0.05)).order_by(sim.desc()).limit(fetch)
    ).all()
    for rank, r in enumerate(rows):
        ranked.setdefault(r.id, {"row": r, "score": 0.0, "d": None})["score"] += 1 / (RRF_K + rank + 1)
    hits = sorted(ranked.values(), key=lambda x: x["score"], reverse=True)[:limit]
    return [
        Hit(
            Source(f"{h['row'].res_model}:{h['row'].res_id}", h["row"].title or h["row"].res_model, h["row"].content),
            h["row"].res_model,
            h["row"].res_id,
            h["score"],
            h["d"],
        )
        for h in hits
    ]


def rebuild(env: Environment, models: list[str] | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, cls in env.registry.models.items():
        if not indexed_fields(cls) or (models and name not in models) or not cls._auto:
            continue
        recs = env[name].elevated().with_context(active_test=False).search([])
        counts[name] = index_records(recs) if recs else 0
    return counts
