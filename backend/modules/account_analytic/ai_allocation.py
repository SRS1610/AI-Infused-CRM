"""``account_analytic.allocation_suggest`` — analytic distribution suggestions for invoice and bill lines (2.6).

The distribution models (:func:`modules.account_analytic.services.distribution_for`) are the company's policy and
the baseline. On top of them, :func:`suggest` learns from the company's **history**: the posted invoice and bill
lines the requesting user can read that carry a distribution (last two years, at most ``HISTORY_LIMIT``). The
``NEIGHBOURS`` lines most similar to the line to allocate vote for the distribution they used, each with the cube
of its similarity (so a few close matches outweigh many loose ones):

==================  =====
same product        3.0
same partner        2.0
same account        1.5
label words         2.0 x Jaccard similarity of the significant words
recency             x 1 for this year, x 0.7 before
==================  =====

The winning distribution fills every analytic plan the models leave empty (the models' plans are kept as they
are). A suggestion needs at least ``MIN_SUPPORT`` neighbours behind it and a share of at least ``MIN_SHARE`` of the
votes; otherwise only the models' distribution (if any) is offered. Accounts must still exist and belong to the
line's company (:func:`validate_distribution`).

The suggestion is a **write proposal** on the line (``analytic_distribution``); nothing changes before the user
accepts it, and accepting runs the ledger's own checks (mandatory plans, posted-line analytic regeneration).
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
import unicodedata
from collections import defaultdict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from modules.account_analytic.models.analytic import normalise_distribution, parse_distribution
from modules.account_analytic.services import distribution_for, validate_distribution
from tessera.ai import Source
from tessera.ai.facts import audit_run, ensure_enabled
from tessera.ai.runtime import handler
from tessera.ai.tools import PendingProposal
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import AccessError, MissingError, UserError, ValidationError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "account_analytic.allocation_suggest"
INVOICE_TYPES = ("out_invoice", "out_refund", "in_invoice", "in_refund")
HISTORY_LIMIT = 2000
HISTORY_DAYS = 730
WEIGHTS = {"product": 3.0, "partner": 2.0, "account": 1.5, "label": 2.0}
NEIGHBOURS = 9
MIN_SUPPORT = 2  # lines among the neighbours that used the winning distribution
MIN_SHARE = 0.4
STOP = {
    "the", "and", "for", "with", "of", "de", "du", "la", "le", "les", "des", "et", "pour", "und", "fur", "mit",
    "der", "die", "das", "en", "au", "aux", "on", "in", "to", "a", "x",
}  # fmt: skip


def words(text: str | None) -> set[str]:
    folded = unicodedata.normalize("NFKD", (text or "").lower()).encode("ascii", "ignore").decode()
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}", folded) if w not in STOP}


def _key(dist: dict[str, float]) -> str:
    return json.dumps(dict(sorted((str(k), round(float(v), 4)) for k, v in dist.items())))


def _features(line: Model) -> dict[str, Any]:
    rec = line.elevated()
    partner = rec.move_id.commercial_partner_id or rec.partner_id
    return {
        "product": rec.product_id.ids[0] if rec.product_id else None,
        "partner": partner.ids[0] if partner else None,
        "account": rec.account_id.ids[0] if rec.account_id else None,
        "words": words(rec.name),
        "date": rec.date or rec.move_id.date,
        "company": rec.move_id.company_id.ids[0],
    }


def history(env: Environment, line: Model) -> list[dict[str, Any]]:
    """Posted invoice / bill lines with a distribution that the user can read (same company, recent first)."""
    f = _features(line)
    since = (f["date"] or env.today()) - dt.timedelta(days=HISTORY_DAYS)
    lines = env["account.move_line"].search(
        [
            ("company_id", "=", f["company"]),
            ("parent_state", "=", "posted"),
            ("display_type", "=", "product"),
            ("move_id.move_type", "in", list(INVOICE_TYPES)),
            ("date", ">=", since),
            ("id", "!=", line.ids[0]),
        ],
        order="date desc, id desc",
        limit=HISTORY_LIMIT,
    )
    out = []
    for h in lines:
        dist = parse_distribution(h.analytic_distribution) if h.analytic_distribution else {}
        if dist:
            out.append({**_features(h), "distribution": {str(k): float(v) for k, v in dist.items()}, "id": h.ids[0]})
    return out


def _similarity(f: dict[str, Any], h: dict[str, Any]) -> tuple[float, list[str]]:
    score, reasons = 0.0, []
    for key in ("product", "partner", "account"):
        if f[key] and f[key] == h[key]:
            score += WEIGHTS[key]
            reasons.append(key)
    if f["words"] and h["words"]:
        jaccard = len(f["words"] & h["words"]) / len(f["words"] | h["words"])
        if jaccard:
            score += WEIGHTS["label"] * jaccard
            reasons.append("label")
    if score and f["date"] and h["date"] and (f["date"] - h["date"]).days > 365:
        score *= 0.7
    return score, reasons


def _by_plan(env: Environment, dist: dict[str, float]) -> dict[int, dict[str, float]]:
    accounts = env["account.analytic_account"].elevated().with_context(active_test=False)
    out: dict[int, dict[str, float]] = defaultdict(dict)
    for acc in accounts.browse([int(k) for k in dist]).exists():
        out[acc.plan_id.ids[0]][str(acc.ids[0])] = float(dist[str(acc.ids[0])])
    return out


def rank(env: Environment, line: Model) -> dict[str, Any]:
    """The models' distribution, the history's candidates (best first) and the combined suggestion."""
    f = _features(line)
    rec = line.elevated()
    ctx = rec._distribution_context()
    models = {str(k): float(v) for k, v in distribution_for(env, ctx).items()}
    votes: dict[str, float] = defaultdict(float)
    support: dict[str, int] = defaultdict(int)
    why: dict[str, set[str]] = defaultdict(set)
    dists: dict[str, dict[str, float]] = {}
    scored = [(s, r, h) for h in history(env, line) for s, r in [_similarity(f, h)] if s]
    scored.sort(key=lambda x: -x[0])
    for score, reasons, h in scored[:NEIGHBOURS]:  # the closest lines vote, closer ones much more (cube)
        key = _key(h["distribution"])
        dists[key] = h["distribution"]
        votes[key] += score**3
        support[key] += 1
        why[key].update(reasons)
    total = sum(votes.values())
    candidates = [
        {
            "distribution": dists[k],
            "votes": round(v, 3),
            "share": round(v / total, 3) if total else 0.0,
            "lines": support[k],
            "reasons": sorted(why[k]),
        }
        for k, v in sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    best = candidates[0] if candidates else None
    confident = bool(best and best["lines"] >= MIN_SUPPORT and best["share"] >= MIN_SHARE)
    suggestion: dict[str, float] = dict(models)
    source = "models" if models else None
    if confident and best is not None:
        covered = set(_by_plan(env, models))
        added = False
        for plan_id, shares in _by_plan(env, best["distribution"]).items():
            if plan_id not in covered:
                suggestion.update(shares)
                added = True
        if added:
            source = "models+history" if models else "history"
    return {
        "models": models,
        "candidates": candidates[:5],
        "confident": confident,
        "suggestion": normalise_distribution(suggestion) or {},
        "source": source,
    }


def _readable_line(line: Model) -> Model:
    line.check_access("read")
    rec = line.elevated()
    if rec.display_type != "product" or rec.move_id.move_type not in INVOICE_TYPES:
        raise UserError(_("Analytic suggestions work on invoice and bill lines"))
    return line


def _can_write(line: Model) -> bool:
    try:
        line.check_access("write")
    except AccessError:
        return False
    return True


def _labels(env: Environment, dist: dict[str, float]) -> list[dict[str, Any]]:
    accounts = env["account.analytic_account"].elevated().with_context(active_test=False)
    out = []
    for acc in accounts.browse([int(k) for k in dist]).exists():
        out.append(
            {"id": acc.ids[0], "name": acc.display_name, "plan": acc.plan_id.name, "percent": dist[str(acc.ids[0])]}
        )
    return out


def suggest(env: Environment, line: Model, *, propose: bool = True) -> dict[str, Any]:
    """The suggested distribution of an invoice or bill line and a write proposal (nothing changes before)."""
    ensure_enabled(env, FEATURE)
    _readable_line(line)
    started = time.monotonic()
    info = rank(env, line)
    rec = line.elevated()
    suggestion = info["suggestion"]
    if suggestion:
        try:
            validate_distribution(env, suggestion, rec.move_id.company_id)
        except ValidationError:
            suggestion = {}
            info["source"] = None
    current = normalise_distribution(rec.analytic_distribution) or {}
    run = audit_run(
        env,
        FEATURE,
        record=line,
        output={"suggestion": suggestion, "source": info["source"], "confident": info["confident"]},
        model="distribution models + weighted history vote",
        started=started,
    )
    proposal = None
    if propose and suggestion and suggestion != current and _can_write(line):
        from tessera.ai import proposals as P
        from tessera.ai.runtime import proposal_dict

        title = str(_("Analytic distribution for '{label}'", label=(rec.name or "")[:60]))
        p = P.create(
            env,
            run,
            FEATURE,
            PendingProposal("write", "account.move_line", line.ids[0], {"analytic_distribution": suggestion}, title),
        )
        run.elevated().write({"decision": "pending"})
        proposal = proposal_dict(p)
    return {
        "run_id": run.ids[0],
        "line_id": line.ids[0],
        "label": rec.name,
        "current": current,
        "suggestion": suggestion,
        "accounts": _labels(env, suggestion),
        "source": info["source"],
        "confident": info["confident"],
        "candidates": info["candidates"],
        "proposal": proposal,
        "ai_generated": True,
    }


def suggest_move(env: Environment, move: Model) -> dict[str, Any]:
    """Suggestions for the product lines of an invoice or bill."""
    ensure_enabled(env, FEATURE)
    move.check_access("read")
    rows = []
    for line in move.elevated().invoice_line_ids.filtered(lambda r: r.display_type == "product"):
        try:
            rows.append(suggest(env, env["account.move_line"].browse(line.ids)))
        except (AccessError, UserError) as exc:
            rows.append({"line_id": line.ids[0], "error": exc.message})
    return {"rows": rows, "ai_generated": True}


@handler(FEATURE)
def explain_context(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    """Optional explanation of a suggestion: the model sees the line, the suggestion and the votes only."""
    if record is None or record._name != "account.move_line":
        raise UserError(_("Choose an invoice or bill line"))
    _readable_line(record)
    info = rank(env, record)
    rec = record.elevated()
    accounts = {a["id"]: a["name"] for a in _labels(env, {k: 0 for c in info["candidates"] for k in c["distribution"]})}

    def describe(dist: dict[str, float]) -> str:
        return ", ".join(f"{accounts.get(int(k), k)} {v:g}%" for k, v in dist.items()) or "none"

    text = "\n".join(
        [
            f"Line: {rec.name or ''}; partner {rec.partner_id.display_name or '-'}; product "
            f"{rec.product_id.display_name or '-'}; account {rec.account_id.display_name or '-'}",
            f"Distribution models: {describe(info['models'])}",
            "History candidates:",
            *[
                f"- {describe(c['distribution'])}: {c['lines']} similar lines ({', '.join(c['reasons'])})"
                for c in info["candidates"]
            ],
        ]
    )
    return {"sources": [Source(f"account.move_line:{record.ids[0]}", "Analytic suggestion", text)]}


# ---------------------------------------------------------------------------------------------- API

router = register_router(APIRouter(prefix="/account_analytic/ai", tags=["account_analytic", "ai"]))


@router.post("/lines/{line_id}/suggest")
def suggest_line(line_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    ensure_enabled(ctx.env, FEATURE)
    line = ctx.env["account.move_line"].browse([line_id])
    if not line.exists():
        raise MissingError(_("No such journal item"))
    return suggest(ctx.env, line)


@router.post("/moves/{move_id}/suggest")
def suggest_document(move_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Suggestions (and proposals) for every product line of an invoice or bill."""
    ensure_enabled(ctx.env, FEATURE)
    move = ctx.env["account.move"].browse([move_id])
    if not move.exists():
        raise MissingError(_("No such document"))
    return suggest_move(ctx.env, move)


# ---------------------------------------------------------------------------------------------- evaluation


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    line = record.elevated().invoice_line_ids.filtered(lambda r: r.display_type == "product")[:1]
    before = normalise_distribution(line.analytic_distribution) or {}
    out = suggest(env, env["account.move_line"].browse(line.ids))
    after = normalise_distribution(line.elevated().analytic_distribution) or {}
    data = {"suggestion": out["suggestion"], "source": out["source"], "mutated": before != after}
    return {"text": "", "data": data, "proposals": 1 if out["proposal"] else 0, "status": "ok", "flags": []}


def _same(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    a, b = a or {}, b or {}
    return set(a) == set(b) and all(abs(float(a[k]) - float(b[k])) < 0.01 for k in a)


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``exact_accuracy``: the whole suggested distribution (accounts and percentages) equals the expected one —
    a partly right split still needs a manual edit, so partial credit is not counted; ``plan_accuracy``: the
    same per analytic plan, for information; ``zero_mutations``: nothing written before acceptance."""
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    exact = sum(1 for r in ok if _same(r["data"]["suggestion"], r["_case"]["expected"]["distribution"]))
    plans_ok = plans = 0
    for r in ok:
        want = r["_case"]["expected"].get("by_plan") or {}
        got = r["data"]["suggestion"] or {}
        for accounts in want.values():
            plans += 1
            sub = {k: got[k] for k in got if k in accounts or k in {str(x) for x in accounts}}
            plans_ok += 1 if _same(sub, accounts) else 0
    return {
        "exact_accuracy": exact / len(ok),
        "plan_accuracy": plans_ok / plans if plans else 1.0,
        "zero_mutations": sum(1 for r in ok if not r["data"]["mutated"]) / len(ok),
    }
