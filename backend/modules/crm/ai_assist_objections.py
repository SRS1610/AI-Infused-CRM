"""``crm.objections`` — suggested answers to a customer objection, grounded in won and lost deals.

Candidate deals are gathered **as the requesting user**, so record rules and the multi-company rule
filter them before anything is ranked: hybrid retrieval over the ``crm.lead`` index (record text and
chatter, vector + trigram, access-filtered by ``tessera.ai.retrieval``) plus a keyword search of closed
deals (lost deals are archived, and the platform does not index archived records, so their lost
reason and feedback are found through the ORM). Only won or lost opportunities are kept; each becomes
a ``crm.lead:<id>`` source, and every citation in the answer is verified against those sources, so a
suggestion can only cite a deal the user can open.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from modules.crm.ai_assist import LEAD, lead_source, lead_text, readable_lead, thread_messages, thread_text
from tessera.ai import Source, retrieval
from tessera.ai.runtime import HANDLERS, RunResult, handler, run_feature
from tessera.ai.tools import ToolContext
from tessera.exceptions import AccessError, MissingError

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "crm.objections"
CLOSED: list[Any] = [("type", "=", "opportunity"), {"any": [["is_won", "=", True], ["lost", "=", True]]}]
STOP = set(
    "about above after again against also because been before being below between both but cannot could does doing "
    "down during each from further have having here into itself just more most much must need only other over own "
    "same should some such than that their them then there these they this those through under until very want "
    "were what when where which while will with would your yours really think thing things going".split()
)
WORD = re.compile(r"[a-zA-ZÀ-ɏ][a-zA-ZÀ-ɏ'-]{3,}")


def keywords(text: str, limit: int = 8) -> list[str]:
    words = [w.lower().strip("'-") for w in WORD.findall(text or "")]
    return list(dict.fromkeys(w for w in words if w not in STOP and len(w) >= 4))[:limit]


def deal_text(env: Environment, deal: Model) -> str:
    if deal.is_won:
        outcome = f"Outcome: WON{f' on {deal.date_closed.date().isoformat()}' if deal.date_closed else ''}"
    else:
        reason = deal.lost_reason_id.name if deal.lost_reason_id else "not recorded"
        outcome = f"Outcome: LOST (reason: {reason})"
    text = f"{outcome}\n{lead_text(deal)}"
    try:
        msgs = thread_messages(env, LEAD, deal.ids[0], 6)
    except (AccessError, MissingError):
        msgs = []
    if msgs:
        text += "\n\nDiscussion (oldest first):\n" + thread_text(msgs)
    return text


def closed_deals(env: Environment, query: str, *, exclude: int | None = None, limit: int = 6) -> list[Source]:
    """Won/lost deals relevant to ``query`` that the user may read, best first."""
    leads = env[LEAD].with_context(active_test=False)
    if not leads.check_access_rights("read", raise_exception=False):
        return []
    hit_rank: dict[int, int] = {}
    for rank, hit in enumerate(retrieval.search(env.with_context(active_test=False), query, models=[LEAD], limit=24)):
        hit_rank.setdefault(hit.res_id, rank)
    words = keywords(query)
    lexical = leads.browse([])
    if words:
        match = [[f, "ilike", w] for w in words for f in ("name", "lost_feedback", "description")]
        lexical = leads.search([*CLOSED, {"any": match}], order="id desc", limit=60)
    ids = (set(hit_rank) | set(lexical.ids)) - {exclude}
    if not ids:
        return []
    deals = leads.search([("id", "in", sorted(ids)), *CLOSED])  # record rules + company filter, as the user
    scored = []
    for deal in deals:
        text = deal_text(env, deal)
        low = text.lower()
        overlap = sum(1 for w in words if w in low) / len(words) if words else 0.0
        bonus = 0.6 / (1 + hit_rank[deal.ids[0]]) if deal.ids[0] in hit_rank else 0.0
        scored.append((overlap + bonus, deal.ids[0], deal, text))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [Source(f"{LEAD}:{d.ids[0]}", d.display_name or "Deal", t) for s, _id, d, t in scored[:limit] if s > 0]


@handler(FEATURE)
def prepare(env: Environment, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    lead = readable_lead(env, record) if record is not None else None
    objection = str(inputs.get("objection") or "")
    deals = closed_deals(env, objection, exclude=lead.ids[0] if lead else None)
    deal_ids = {s.id for s in deals}
    current = [lead_source(env, lead)] if lead else []
    if current:
        current[0].title = f"Current deal: {current[0].title}"

    def on_output(ctx: ToolContext, _text: str, data: Any) -> None:
        """Per-suggestion citations may only name past deals the model was given."""
        if not isinstance(data, dict):
            return
        for item in data.get("responses") or []:
            if isinstance(item, dict):
                cited = [c for c in item.get("citations") or [] if isinstance(c, str)]
                item["citations"] = [c for c in cited if c in deal_ids and c in ctx.sources]
                item["dropped_citations"] = len(cited) - len(item["citations"])
        data["citations"] = [c for c in data.get("citations") or [] if c in deal_ids]
        data["deals_considered"] = len(deal_ids)

    return {"sources": [*current, *deals], "on_output": on_output}


# ---------------------------------------------------------------------------------------------- evaluation


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> RunResult:
    """Index the case's deals (the indexer normally runs as a background job after commit), run the
    feature, then check that every citation resolves to a record the user can open."""
    sys_env = env.elevated("ai eval index")
    retrieval.rebuild(sys_env, [LEAD])
    retrieval.index_messages(sys_env, sys_env["chatter.message"].search([("model", "=", LEAD)]))
    result = run_feature(env, FEATURE, inputs, record=record, **HANDLERS[FEATURE](env, record, inputs))
    data = result.data if isinstance(result.data, dict) else {}
    cited = list(data.get("citations") or [])
    for item in data.get("responses") or []:
        cited += item.get("citations") or []
    unresolved = 0
    for ref in dict.fromkeys(cited):
        model, _, res_id = ref.rpartition(":")
        try:
            env[model].with_context(active_test=False).browse([int(res_id)]).check_access("read")
        except (AccessError, MissingError, ValueError, KeyError):
            unresolved += 1
    result.data = {**data, "_eval": {"cited": len(set(cited)), "unresolved": unresolved}}
    return result


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    cited = unresolved = 0
    for r in results:
        ev = (r.get("data") or {}).get("_eval") if isinstance(r.get("data"), dict) else None
        if ev:
            cited += ev["cited"]
            unresolved += ev["unresolved"]
    return {"citations_resolvable": 1.0 - unresolved / cited if cited else 1.0, "citations_total": float(cited)}
