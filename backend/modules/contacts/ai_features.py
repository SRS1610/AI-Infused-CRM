"""Contacts AI features (Phase 1.6): duplicate detection, tag & hierarchy suggestions, enrichment.

Every context builder reads **as the requesting user**: record rules, the multi-company rule and
field groups decide what reaches a prompt. Model output is post-processed by code that cannot be
talked out of its checks (only existing tags, only parent companies that were offered, only values
an approved adapter actually returned) and every change is a proposal a person accepts.

Evaluation notes (honesty): ``contacts.duplicates`` is deterministic, so its precision/recall are
measured for real on the labelled set. ``contacts.tag_suggest`` and ``contacts.enrich`` call a model;
in CI their model answers are replayed from hand-written cassettes, so the replay scores measure the
pipeline (context building, filtering, post-processing, proposals) — not model quality, which the
nightly live run measures.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from modules.contacts import duplicates as D
from modules.contacts import enrichment as E
from tessera.ai import Source
from tessera.ai.errors import AIError
from tessera.ai.prompts import html_to_text
from tessera.ai.runtime import check_enabled, get_feature, handler, run_feature
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import AccessError, MissingError
from tessera.i18n import _
from tessera.jobs import enqueue, job
from tessera.logging import get_logger
from tessera.orm.hooks import hook

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)
TAG_FEATURE = "contacts.tag_suggest"
ENRICH_FEATURE = "contacts.enrich"
DUP_FEATURE = D.FEATURE
MIN_TAG_CONFIDENCE = 0.5


def feature_enabled(env: Environment, name: str) -> bool:
    if "ai_core.feature" not in env.registry.models:
        return False
    try:
        check_enabled(env, get_feature(env, name))
    except (AIError, AccessError, MissingError):
        return False
    return True


def _safe(fn: Callable[[], Any], default: Any = "") -> Any:
    try:
        return fn()
    except AccessError:
        return default


def partner_text(p: Model, *, notes: bool = True) -> str:
    """What the user can see of a contact, as plain text for a prompt."""
    lines = [f"Name: {p.name or ''}", f"Kind: {'company' if p.is_company else 'person'}"]
    parent = _safe(lambda: p.parent_id.display_name if p.parent_id else "")
    for label, value in (
        ("Company", parent),
        ("Job position", p.function),
        ("Industry", _safe(lambda: p.industry_id.name if p.industry_id else "")),
        ("City", p.city),
        ("Country", _safe(lambda: p.country_id.name if p.country_id else "")),
        ("Website", p.website),
        ("E-mail domain", D.email_domain(p.email_normalized)),
        ("Tags", ", ".join(_safe(lambda: p.tag_ids.mapped("name"), []))),
    ):
        if value:
            lines.append(f"{label}: {value}")
    if notes and p.comment:
        lines.append(f"Notes: {html_to_text(p.comment).strip()[:800]}")
    return "\n".join(lines)


# -- duplicates (deterministic; the model only explains) -------------------------------------------


@handler(DUP_FEATURE)
def duplicates_context(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    """Context for the optional explanation of the detected duplicates (``/ai/features/.../run``)."""
    record.check_access("read")
    found = D.detect(record, limit=5)
    sources = [Source(f"contacts.partner:{record.ids[0]}", record.display_name or "", partner_text(record))]
    for c in found["candidates"]:
        p = env["contacts.partner"].browse([c["id"]])
        text = partner_text(p) + f"\nDuplicate score: {c['score']:.2f}\nSignals: {'; '.join(c['reasons']) or 'none'}"
        sources.append(Source(f"contacts.partner:{c['id']}", c["display_name"] or "", text))
    return {"sources": sources}


@job("contacts.check_duplicates", queue="ai", max_attempts=2, backoff_seconds=60)
def check_duplicates_job(env: Environment, partner_id: int) -> int:
    """Background check after a contact is created; notifies its creator when duplicates turn up."""
    if not feature_enabled(env, DUP_FEATURE):
        return 0
    partner = env["contacts.partner"].browse([partner_id]).exists()
    if not partner:
        return 0
    found = D.detect(partner, limit=5)
    dups = [c for c in found["candidates"] if c["duplicate"]]
    if dups:
        names = ", ".join(str(c["display_name"]) for c in dups[:3])
        env["core.notification"].notify(
            [env.uid],
            _("Possible duplicate: {name}", name=partner.display_name),
            _("Looks like {names}. Review and merge from the contact.", names=names),
            kind="system",
            model="contacts.partner",
            res_id=partner_id,
            url=f"/m/contacts.partner/{partner_id}",
        )
    return len(dups)


@hook("after_create")
def queue_duplicate_check(records: Model, vals_list: list[dict[str, Any]]) -> None:
    """New contacts created by a person get a background duplicate check (imports and system-created
    contacts are checked on demand instead)."""
    env = records.env
    if records._name != "contacts.partner" or env.su or env.context.get("skip_ai_duplicates"):
        return
    if "core.job" not in env.registry.models or "ai_core.feature" not in env.registry.models or len(records) > 20:
        return
    for p in records:
        if p.type == "contact" and p.name:
            enqueue(env, "contacts.check_duplicates", {"partner_id": p.ids[0]}, description="Duplicate check")


# -- tags and parent company -----------------------------------------------------------------------


def _fold(value: Any) -> str:
    return D.fold(str(value or "")).strip()


def similar_partners(me: Model, limit: int = 6) -> list[Model]:
    """Tagged contacts the user can read that resemble ``me`` (same industry, company, domain, place)."""
    partners = me.env["contacts.partner"]
    domain: list[Any] = [("id", "!=", me.ids[0]), ("tag_ids", "is_set")]
    ors: list[Any] = []
    if me.industry_id:
        ors.append(["industry_id", "=", me.industry_id.ids[0]])
    if me.commercial_partner_id and not me.is_company:
        ors.append(["commercial_partner_id", "=", me.commercial_partner_id.ids[0]])
    for dom in D.business_domains(me):
        ors.append(["email_normalized", "like", f"%@{dom}"])
    if me.country_id:
        ors.append(["country_id", "=", me.country_id.ids[0]])
    for tok in [t for t in D.name_tokens(me.name) if len(t) >= 4][:3]:
        ors.append(["name", "ilike", tok])
    if not ors:
        return []
    found = partners.search([*domain, {"any": ors}], limit=60)
    mine_tokens = set(D.name_tokens(me.name))
    my_domains = D.business_domains(me)

    def score(p: Model) -> float:
        s = 0.0
        s += 3 if me.industry_id and p.industry_id == me.industry_id else 0
        s += 3 if me.commercial_partner_id and p.commercial_partner_id == me.commercial_partner_id else 0
        s += 2 if my_domains & D.business_domains(p) else 0
        s += 1 if me.country_id and p.country_id == me.country_id else 0
        s += 1 if me.city and _fold(p.city) == _fold(me.city) else 0
        theirs = set(D.name_tokens(p.name))
        s += 2 * len(mine_tokens & theirs) / max(1, len(mine_tokens | theirs))
        return s

    ranked = sorted(found, key=lambda p: (-score(p), p.ids[0]))
    return [p for p in ranked if score(p) > 0][:limit]


def parent_candidates(me: Model, limit: int = 3) -> list[Model]:
    """Companies the user can read that this person probably works for (shared business domain)."""
    if me.is_company or me.parent_id:
        return []
    doms = D.business_domains(me)
    if not doms:
        return []
    ors = [["email_normalized", "like", f"%@{d}"] for d in doms] + [["website", "ilike", d] for d in doms]
    found = me.env["contacts.partner"].search(
        [("is_company", "=", True), ("id", "!=", me.ids[0]), {"any": ors}], limit=limit
    )
    return list(found)


@handler(TAG_FEATURE)
def tag_suggest_context(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    record.check_access("read")
    tags = env["contacts.tag"].search([])
    similar = similar_partners(record)
    parents = parent_candidates(record)
    catalogue = ", ".join(t.name for t in tags)
    main = partner_text(record) + f"\n\nTags that exist (choose only from these): {catalogue}"
    if parents:
        main += "\nPossible parent companies: " + "; ".join(f"#{p.ids[0]} {p.display_name}" for p in parents)
    sources = [Source(f"contacts.partner:{record.ids[0]}", record.display_name or "", main)]
    for p in [*similar, *parents]:
        if all(s.res_id != p.ids[0] for s in sources):
            sources.append(Source(f"contacts.partner:{p.ids[0]}", p.display_name or "", partner_text(p, notes=False)))
    return {"sources": sources, "on_output": _tag_output(record, tags, [p.ids[0] for p in parents])}


def _tag_output(record: Model, tags: Model, parent_ids: list[int]) -> Callable[[ToolContext, str, Any], None]:
    def hook_(ctx: ToolContext, text: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        by_name = {_fold(t.name): t for t in tags}
        current = set(record.tag_ids.ids)
        suggested: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        for item in data.get("tags") or []:
            name = str(item.get("name", ""))
            tag = by_name.get(_fold(name))
            if tag is None:
                dropped.append({"name": name, "why": "not an existing tag"})
            elif tag.ids[0] in current:
                dropped.append({"name": name, "why": "already set"})
            elif all(s["tag_id"] != tag.ids[0] for s in suggested):
                suggested.append(
                    {
                        "tag_id": tag.ids[0],
                        "name": tag.name,
                        "confidence": round(float(item.get("confidence") or 0), 2),
                        "reason": str(item.get("reason") or ""),
                    }
                )
        parent = None
        pid = data.get("parent_id")
        if pid:
            if pid in parent_ids and not record.parent_id and not record.is_company:
                p = record.env["contacts.partner"].browse([pid])
                parent = {"id": pid, "display_name": p.display_name, "reason": str(data.get("parent_reason") or "")}
            else:
                dropped.append({"parent_id": pid, "why": "not one of the companies offered"})
        payload: dict[str, Any] = {}
        chosen = [s for s in suggested if s["confidence"] >= MIN_TAG_CONFIDENCE][:3]
        if chosen:
            payload["tag_ids"] = [["add", s["tag_id"]] for s in chosen]
        if parent:
            payload["parent_id"] = parent["id"]
        data.update(suggested=suggested, parent=parent, dropped=dropped, payload=payload)
        if payload and _can_write(record):
            what = " and ".join(
                x for x in (", ".join(s["name"] for s in chosen), parent and f"company {parent['display_name']}") if x
            )
            ctx.proposals.append(
                PendingProposal(
                    "write", "contacts.partner", record.ids[0], payload, f"Add {what} to {record.display_name}"
                )
            )

    return hook_


def _can_write(record: Model) -> bool:
    try:
        record.check_access("write")
    except AccessError:
        return False
    return True


# -- enrichment ------------------------------------------------------------------------------------


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def same_value(field: str, a: Any, b: Any) -> bool:
    if field == "phone":
        da, db = _digits(str(a)), _digits(str(b))
        return bool(da) and (da == db or da[-9:] == db[-9:])
    if field == "website":
        return bool(a) and D.website_domain(str(a)) == D.website_domain(str(b))
    if field in ("vat", "zip"):
        return bool(a) and re.sub(r"\W", "", str(a)).upper() == re.sub(r"\W", "", str(b or "")).upper()
    if field == "logo_url":
        return bool(a) and _link(str(a)) == _link(str(b or ""))
    if field == "social_links":
        return bool(a) and {_link(x) for x in str(a).split()} == {_link(x) for x in str(b or "").split()}
    return bool(a) and _fold(a) == _fold(b)


def _link(url: str) -> str:
    return re.sub(r"^https?://(www\.)?", "", url.strip().lower()).rstrip("/")


TARGET = {"country": "country_id", "industry": "industry_id"}


def _current(record: Model, field: str) -> str:
    rec = record
    if field == "country":
        return rec.country_id.code if rec.country_id else ""
    if field == "industry":
        return _safe(lambda: rec.industry_id.name if rec.industry_id else "")
    return str(rec[field] or "")


def _convert(env: Environment, field: str, value: str) -> Any:
    if field == "country":
        country = env["core.country"].search([("code", "=", value.strip().upper())], limit=1)
        if not country and len(value.strip()) > 2:  # websites often give the country's name
            named = env["core.country"].search([("name", "ilike", value.strip())], limit=5)
            country = named.filtered(lambda c: _fold(c.name) == _fold(value))[:1]
        return country.ids[0] if country else None
    if field == "industry":
        industry = env["contacts.industry"].search([("name", "=", value.strip())], limit=1)
        return industry.ids[0] if industry else None
    return value.strip()


@handler(ENRICH_FEATURE)
def enrich_context(env: Environment, record: Model, inputs: dict[str, Any]) -> dict[str, Any]:
    record.check_access("read")
    current = "\n".join(f"{f}: {_current(record, f) or '(empty)'}" for f in E.FIELDS)
    notes = f"\nNotes: {html_to_text(record.comment).strip()[:800]}" if record.comment else ""
    sources = [
        Source(
            f"contacts.partner:{record.ids[0]}",
            record.display_name or "",
            f"Name: {record.name}\nE-mail: {record.email or ''}\nCurrent values:\n{current}{notes}",
        )
    ]
    allowed: dict[str, list[E.EnrichmentResult]] = {}
    for src, results in E.lookup(record):
        sid = f"contacts.enrichment_source:{src.ids[0]}"
        allowed[sid] = results
        lines = [f"{r.entity} | {r.field}: {r.value} | {r.source_url}" for r in results]
        sources.append(Source(sid, src.name, "\n".join(lines) if lines else "No matching entry."))
    return {"sources": sources, "on_output": _enrich_output(record, allowed)}


def _enrich_output(record: Model, allowed: dict[str, list[E.EnrichmentResult]]) -> Callable[..., None]:
    def hook_(ctx: ToolContext, text: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        env = ctx.env
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []
        payload: dict[str, Any] = {}
        raw = list(data.get("suggestions") or [])
        for item in raw:
            field, value, sid = str(item.get("field", "")), str(item.get("value", "")), str(item.get("source", ""))
            why = None
            support = [r for r in allowed.get(sid, []) if r.field == field and same_value(field, r.value, value)]
            target = TARGET.get(field, field)
            if field not in E.FIELDS:
                why = "unknown field"
            elif sid not in allowed:
                why = "no approved source cited"
            elif not support:
                why = "value not found in the cited source"
            elif same_value(field, value, _current(record, field)):
                why = "already set"
            elif target in payload:
                why = "duplicate suggestion"
            elif not record._check_field_access(record._fields[target], "write"):
                why = "field not writable"
            converted = _convert(env, field, value) if why is None else None
            if why is None and converted is None:
                why = f"unknown {field}"
            if why:
                dropped.append({"field": field, "value": value, "source": sid, "why": why})
                continue
            payload[target] = converted
            kept.append(
                {
                    "field": field,
                    "target": target,
                    "value": value,
                    "current": _current(record, field) or None,
                    "source": sid,
                    "source_title": ctx.sources[sid].title if sid in ctx.sources else sid,
                    "source_url": support[0].source_url,
                    "entity": support[0].entity,
                    "entities": sorted({r.entity for r in support}),
                }
            )
        # one proposal describes one outside entity: when results of several entities were mixed, keep
        # the entity that supports most of the values and drop the rest
        if len({k["entity"] for k in kept}) > 1:
            counts: dict[str, int] = {}
            for k in kept:
                for ent in k["entities"]:
                    counts[ent] = counts.get(ent, 0) + 1
            best = max(sorted(counts), key=lambda e: counts[e])
            for k in [k for k in kept if best not in k["entities"]]:
                kept.remove(k)
                payload.pop(k["target"], None)
                dropped.append(
                    {"field": k["field"], "value": k["value"], "source": k["source"], "why": "another entity"}
                )
        for k in kept:
            k.pop("entities", None)
        data.update(suggestions=kept, dropped=dropped, payload=payload, raw_count=len(raw))
        if payload and _can_write(record):
            ctx.proposals.append(
                PendingProposal(
                    "write",
                    "contacts.partner",
                    record.ids[0],
                    payload,
                    f"Enrich {record.display_name} ({len(kept)} fields)",
                )
            )

    return hook_


@job("contacts.enrich_partner", queue="ai", max_attempts=1)
def enrich_partner_job(env: Environment, partner_id: int) -> dict[str, Any]:
    """Background enrichment (as the user who asked); the result is a proposal plus a notification."""
    partner = env["contacts.partner"].browse([partner_id]).exists()
    if not partner:
        return {"suggestions": 0}
    url = f"/m/contacts.partner/{partner_id}"
    try:
        result = run_feature(env, ENRICH_FEATURE, {}, record=partner, **enrich_context(env, partner, {}))
    except AIError as exc:
        env["core.notification"].notify(
            [env.uid],
            _("Enrichment unavailable: {name}", name=partner.display_name),
            exc.message,
            kind="job",
            url=url,
        )
        return {"error": exc.code}
    n = len(result.data.get("suggestions", [])) if isinstance(result.data, dict) else 0
    env["core.notification"].notify(
        [env.uid],
        _("Enrichment ready: {name}", name=partner.display_name),
        (
            _("{count} suggested fields, each with its source. Review them on the contact.", count=n)
            if n > 1
            else _("1 suggested field, with its source. Review it on the contact.")
        )
        if n
        else _("No new information was found in the approved sources."),
        kind="job",
        model="contacts.partner",
        res_id=partner_id,
        url=url,
    )
    return {"run_id": result.run.ids[0], "suggestions": n}


# -- evaluation entries and aggregate scorers ------------------------------------------------------


def unreadable_sources(env: Environment, sources: list[Source]) -> list[str]:
    """Source ids whose record the user cannot read (must always be empty: context is built as the user)."""
    bad = []
    for src in sources:
        try:
            if src.model not in env.registry.models:
                raise MissingError(src.model)
            rec = env[src.model].browse([src.res_id])
            if not rec.exists():
                raise MissingError(src.id)
            rec.check_access("read")
        except (AccessError, MissingError):
            bad.append(src.id)
    return bad


def audited_entry(feature: str) -> Callable[..., Any]:
    """Eval entry: the API path (handler, then ``run_feature``) plus an audit of every source the
    handler put in the prompt; a source the user cannot read flags the case ``unreadable_source``."""

    def entry(env: Environment, inputs: dict[str, Any], record: Model) -> Any:
        from tessera.ai.runtime import HANDLERS

        kwargs = HANDLERS[feature](env, record, inputs)
        bad = unreadable_sources(env, kwargs.get("sources") or [])
        result = run_feature(env, feature, inputs, record=record, **kwargs)
        if bad:
            result.flags.append("unreadable_source")
        return result

    return entry


tag_suggest_entry = audited_entry(TAG_FEATURE)
enrich_entry = audited_entry(ENRICH_FEATURE)


def leak_count(results: list[dict[str, Any]]) -> int:
    return sum(1 for r in results if "unreadable_source" in (r.get("flags") or []))


def duplicates_entry(env: Environment, inputs: dict[str, Any], record: Model) -> dict[str, Any]:
    """Deterministic: what the panel shows for ``record`` (as the case's user)."""
    found = D.detect(record, limit=20)
    return {"text": "", "data": found, "proposals": 0, "status": "ok"}


def duplicates_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    tp = fp = fn = 0
    for r in results:
        if r.get("status") != "ok":
            continue
        predicted = set((r.get("data") or {}).get("duplicates") or [])
        expected = set(r["_case"]["expected"].get("duplicates") or [])
        tp += len(predicted & expected)
        fp += len(predicted - expected)
        fn += len(expected - predicted)
    precision = tp / (tp + fp) if tp + fp else 1.0
    recall = tp / (tp + fn) if tp + fn else 1.0
    return {"precision": precision, "recall": recall, "pairs_tp": tp, "pairs_fp": fp, "pairs_fn": fn}


def tags_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    hits = total = parent_hits = parent_total = 0
    for r in results:
        exp = r["_case"]["expected"]
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        if "tag" in exp:
            total += 1
            top3 = [_fold(s["name"]) for s in (data.get("suggested") or [])[:3]]
            hits += 1 if _fold(exp["tag"]) in top3 else 0
        if "parent" in exp:
            parent_total += 1
            got = (data.get("parent") or {}).get("id")
            parent_hits += 1 if got == exp["parent"] else 0
    out = {"top3_accuracy": hits / total if total else 0.0, "unreadable_sources": leak_count(results)}
    if parent_total:
        out["parent_accuracy"] = parent_hits / parent_total
    return out


def enrich_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    """``field_accuracy``: correct suggested values over (expected or suggested fields), pooled;
    ``attribution``: share of kept suggestions that carry an approved source and a link."""
    correct = denominator = attributed = kept_total = raw_total = 0
    for r in results:
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        expected: dict[str, str] = r["_case"]["expected"].get("enriched") or {}
        got = {s["field"]: s["value"] for s in data.get("suggestions") or []}
        for f in set(expected) | set(got):
            denominator += 1
            correct += 1 if f in expected and f in got and same_value(f, got[f], expected[f]) else 0
        for s in data.get("suggestions") or []:
            kept_total += 1
            attributed += (
                1 if str(s.get("source", "")).startswith("contacts.enrichment_source:") and s.get("source_url") else 0
            )
        raw_total += int(data.get("raw_count") or 0)
    return {
        "field_accuracy": correct / denominator if denominator else 1.0,
        "attribution": attributed / kept_total if kept_total else 1.0,
        "unreadable_sources": leak_count(results),
        "suggestions_kept": kept_total,
        "suggestions_dropped": raw_total - kept_total,
    }
