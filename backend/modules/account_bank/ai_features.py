"""``account_bank.reconcile_suggest`` — reconciliation suggestions (sub-phase 2.6, BUILD_PLAN §3).

The reconciliation rules (:func:`modules.account_bank.rules.suggest_matches`) find candidates by exact
reference, partner and amount. This feature re-ranks and extends them for the transactions the rules leave
open (acceptance criterion 2-d: "the remainder are proposed by AI"):

* **candidates**: the rules' candidates, plus open items whose reference *looks like* the label (typos,
  missing separators, the bare serial number), whose partner the label names (fuzzy) or the counterparty's
  history designates, or whose amount is close (bank fees deducted), plus groups of one partner's items that
  add up to the amount;
* **features** per candidate: amount (exact / close), reference similarity, partner (same, named, history),
  date (days from the due date) and the rules' own score; a fixed logistic score combines them (weights
  below, tuned on a development set generated with another seed than the evaluation set);
* **a model only for ambiguous text**: when the two best candidates are within ``AMBIGUOUS_MARGIN`` and the
  transaction has a label, the small-tier model is asked which of the offered candidates the label means.
  Its answer must be one of the offered indexes, otherwise the deterministic order stands.

Matching stays a decision of the user: the best candidate becomes an ``action`` proposal
(``account_bank.match``) that runs the ledger's own matching (:func:`modules.account_bank.matching.match_line`)
as the accepting user. Nothing is reconciled before that.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from itertools import combinations
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from modules.account_bank import rules as R
from tessera.ai import Source
from tessera.ai.errors import AIError
from tessera.ai.facts import audit_run, ensure_enabled, feature_enabled
from tessera.ai.proposals import action
from tessera.ai.tools import PendingProposal
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import AccessError, MissingError, UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "account_bank.reconcile_suggest"
ACTION = "account_bank.match"
AMBIGUOUS_MARGIN = 0.05
PROPOSE_MIN = 0.5
MAX_OPEN = 400
WEIGHTS = {"bias": -4.2, "amount": 3.4, "reference": 3.6, "partner": 2.4, "date": 0.9, "rules": 1.2, "group": 0.3}
HISTORY_LIMIT = 500
LEGAL = {"SA", "SAS", "SARL", "GMBH", "AG", "BV", "NV", "LTD", "LLC", "INC", "CORP", "CO", "THE", "AND", "PLC", "SPA"}


def _norm(text: str | None) -> str:
    return re.sub(r"[^0-9A-Z]", "", (text or "").upper())


def _tokens(text: str | None) -> list[str]:
    return [t for t in re.findall(r"[A-Z0-9]{2,}", (text or "").upper()) if t not in LEGAL]


def reference_similarity(ref: str, text: str) -> float:
    """How strongly ``text`` (a bank label) refers to the document reference ``ref`` (0-1).

    Exact (normalised) containment is 1.0; otherwise the best fuzzy window match, or the document's serial
    number with its year (``2026 … 12`` for ``INV/2026/00012``)."""
    r, t = _norm(ref), _norm(text)
    if len(r) < 3 or not t:
        return 0.0
    if r in t:
        return 1.0
    best = 0.0
    for size in {max(3, len(r) - 2), len(r) - 1, len(r), len(r) + 1, len(r) + 2}:
        for i in range(0, max(1, len(t) - size + 1)):
            window = t[i : i + size]
            if window[:1] != r[:1] and window[-1:] != r[-1:]:
                continue
            best = max(best, SequenceMatcher(None, r, window).ratio())
    best = best if best >= 0.75 else 0.0
    groups = re.findall(r"\d+", ref)
    digits = [g.lstrip("0") for g in re.findall(r"\d+", text)]
    if groups:
        serial = groups[-1].lstrip("0")
        years = [g for g in groups[:-1] if len(g) == 4]
        if serial and serial in digits:
            if years and any(y in digits or y[2:] in digits for y in years):
                best = max(best, 0.85)
            elif len(serial) >= 3:
                best = max(best, 0.6)
            else:
                best = max(best, 0.25)
        joined = "".join(groups).lstrip("0")
        if len(joined) >= 5 and joined in "".join(re.findall(r"\d+", text)):
            best = max(best, 0.85)
    return round(best, 4)


def name_similarity(name: str | None, text: str | None) -> float:
    """Share of the partner name's significant tokens found (fuzzily) in ``text``."""
    wanted = _tokens(name)
    have = _tokens(text)
    if not wanted or not have:
        return 0.0
    found = 0.0
    for w in wanted:
        score = max(SequenceMatcher(None, w, h).ratio() for h in have)
        found += 1.0 if score >= 0.85 else 0.0
    return round(found / len(wanted), 4)


@dataclass
class Suggestion:
    items: Model
    amount: float
    features: dict[str, float]
    reasons: list[str]
    rules_score: float = 0.0
    score: float = 0.0
    writeoffs: list[dict[str, Any]] = field(default_factory=list)
    kind: str = "items"
    model_name: str | None = None

    @property
    def key(self) -> tuple[int, ...]:
        return tuple(sorted(self.items.ids)) if self.kind == "items" else (-1,)

    def to_dict(self, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "kind": self.kind,
            "line_ids": list(self.items.ids) if self.kind == "items" else [],
            "items": [R.item_payload(i) for i in self.items] if self.kind == "items" else [],
            "amount": round(self.amount, 2),
            "score": round(self.score, 4),
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "reasons": self.reasons,
            "writeoffs": self.writeoffs,
            "rule": self.model_name,
        }


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class History:
    """Counterparties seen on reconciled transactions of the company: account number / name → partner."""

    def __init__(self, st_line: Model) -> None:
        self.by_account: dict[str, dict[int, int]] = {}
        self.by_name: dict[str, dict[int, int]] = {}
        env = st_line.env
        done = (
            env["account.bank_statement_line"]
            .elevated("bank history")
            .search(
                [
                    ("company_id", "=", st_line.company_id.ids[0]),
                    ("state", "=", "reconciled"),
                    ("id", "!=", st_line.ids[0]),
                ],
                order="date desc, id desc",
                limit=HISTORY_LIMIT,
            )
        )
        lines = env["account.move_line"].elevated("bank history")
        for past in done:
            items = [int(i["line_id"]) for i in (past.match_data or {}).get("items", [])]
            partners = {p for p in lines.browse(items).exists().mapped("partner_id") for p in [p] if p}
            partner = past.partner_id or (next(iter(partners)) if len(partners) == 1 else None)
            if not partner:
                continue
            pid = (partner.commercial_partner_id or partner).ids[0]
            if past.account_number:
                slot = self.by_account.setdefault(_norm(past.account_number), {})
                slot[pid] = slot.get(pid, 0) + 1
            if past.partner_name:
                slot = self.by_name.setdefault(_norm(past.partner_name), {})
                slot[pid] = slot.get(pid, 0) + 1

    def partner_for(self, st_line: Model) -> int | None:
        for table, key in ((self.by_account, st_line.account_number), (self.by_name, st_line.partner_name)):
            counts = table.get(_norm(key)) if key else None
            if counts:
                best = max(counts.values())
                winners = [p for p, n in counts.items() if n == best]
                if len(winners) == 1:
                    return winners[0]
        return None


def _commercial(partner: Model) -> int | None:
    if not partner:
        return None
    p = partner.elevated()
    return (p.commercial_partner_id or p).ids[0]


def _features(st_line: Model, items: Model, amount: float, open_amount: float, history_pid: int | None) -> Suggestion:
    text = R._text_of(st_line)
    name_text = f"{st_line.partner_name or ''} {text}"
    feats: dict[str, float] = {}
    reasons: list[str] = []
    diff = abs(abs(open_amount) - abs(amount))
    if diff < 0.005:
        feats["amount"] = 1.0
        reasons.append("amount_exact")
    elif abs(open_amount) and diff / abs(open_amount) <= 0.03 and abs(amount) > abs(open_amount):
        feats["amount"] = round(0.6 * (1 - diff / abs(open_amount) / 0.03), 4)
        reasons.append("amount_close")
    else:
        feats["amount"] = 0.0
    ref = 0.0
    for item in items:
        best = max((reference_similarity(r, text) for r in R._references(item)), default=0.0)
        ref += best / len(items)
    feats["reference"] = round(ref, 4)
    if ref >= 0.99:
        reasons.append("reference")
    elif ref >= 0.5:
        reasons.append("reference_similar")
    line_pid = _commercial(st_line.partner_id)
    pids = {_commercial(i.partner_id) for i in items}
    partner = 0.0
    if line_pid and pids == {line_pid}:
        partner = 1.0
        reasons.append("partner")
    elif len(pids) == 1 and None not in pids:
        pid = next(iter(pids))
        named = name_similarity(items[:1].partner_id.elevated().name, name_text)
        if named >= 0.99:
            partner = 0.9
            reasons.append("partner_named")
        elif history_pid and history_pid == pid:
            partner = 0.85
            reasons.append("partner_history")
        elif named >= 0.5:
            partner = 0.5 * named
            reasons.append("partner_named")
    feats["partner"] = partner
    due = [i.date_maturity or i.date for i in items if i.date_maturity or i.date]
    date_score = 0.0
    if due and st_line.date:
        if any((i.date or st_line.date) > st_line.date for i in items):
            date_score = 0.0
        else:
            days = abs((st_line.date - max(due)).days)
            date_score = math.exp(-days / 45.0)
    feats["date"] = round(date_score, 4)
    feats["group"] = 1.0 if len(items) > 1 else 0.0
    return Suggestion(items=items, amount=amount, features=feats, reasons=reasons)


def _score(s: Suggestion) -> float:
    w = WEIGHTS
    x = w["bias"] + sum(w[k] * s.features.get(k, 0.0) for k in ("amount", "reference", "partner", "date", "group"))
    x += w["rules"] * s.rules_score
    return round(_sigmoid(x), 4)


def rank(st_line: Model, limit: int = 5) -> dict[str, Any]:
    """Ranked suggestions for a transaction (deterministic part; no model call)."""
    line = st_line.elevated("bank suggestions")
    open_amount = R._open_amount(line)
    if not open_amount or line.state == "reconciled":
        return {"suggestions": [], "ambiguous": False, "open_amount": open_amount}
    history = History(line)
    history_pid = history.partner_for(line)
    out: dict[tuple[int, ...], Suggestion] = {}

    def consider(items: Model, rules_score: float = 0.0, rule: Any = None, writeoffs: Any = None) -> None:
        amount = sum(R._in_line_currency(line, i) for i in items)
        if not amount or (amount > 0) != (open_amount > 0):
            return
        s = _features(line, items, amount, open_amount, history_pid)
        s.rules_score = rules_score
        s.writeoffs = list(writeoffs or [])
        s.model_name = rule.name if rule is not None else None
        prev = out.get(s.key)
        if prev is None or prev.rules_score < rules_score:
            out[s.key] = s

    rule_cands = R.suggest_matches(line, limit=20)
    writeoff_cands = []
    for c in rule_cands:
        if c.kind == "items" and c.items:
            consider(c.items, c.score, c.model, c.writeoffs if "amount_tolerance" in c.reasons else None)
        elif c.kind == "writeoff":
            writeoff_cands.append(c)
    items = R._open_items(line)[:MAX_OPEN]
    text = R._text_of(line)
    name_text = f"{line.partner_name or ''} {text}"
    line_pid = _commercial(line.partner_id) or history_pid
    by_partner: dict[int, list[Model]] = {}
    for item in items:
        residual = R._in_line_currency(line, item)
        if not residual or (residual > 0) != (open_amount > 0):
            continue
        pid = _commercial(item.partner_id)
        diff = abs(abs(open_amount) - abs(residual))
        close = diff < 0.005 or (abs(residual) > abs(open_amount) and diff / abs(open_amount) <= 0.03)
        similar = text and max((reference_similarity(r, text) for r in R._references(item)), default=0) >= 0.25
        named = pid is not None and (
            pid == line_pid or name_similarity(item.partner_id.elevated().name, name_text) >= 0.5
        )
        if close or similar or named:
            consider(item)
        if pid is not None and named:
            by_partner.setdefault(pid, []).append(item)
    for group in by_partner.values():
        group = group[:10]
        for size in (2, 3):
            for combo in combinations(group, size):
                total = sum(R._in_line_currency(line, i) for i in combo)
                if abs(abs(total) - abs(open_amount)) < 0.005:
                    recs = combo[0]
                    for c in combo[1:]:
                        recs = recs | c
                    consider(recs)
    ranked = list(out.values())
    for s in ranked:
        s.score = _score(s)
    for c in writeoff_cands:
        ranked.append(
            Suggestion(
                items=line.env["account.move_line"],
                amount=open_amount,
                features={"rules": c.score},
                reasons=list(c.reasons),
                rules_score=c.score,
                score=round(min(c.score, 0.6), 4),
                writeoffs=list(c.writeoffs),
                kind="writeoff",
                model_name=c.model.name if c.model else None,
            )
        )

    def order(s: Suggestion) -> tuple[Any, ...]:
        first = s.items[:1]
        due = (first.date_maturity or first.date) if first else None
        return (-s.score, -s.features.get("reference", 0.0), due.toordinal() if due else 10**7, s.key)

    ranked.sort(key=order)
    ranked = ranked[: max(1, int(limit))]
    ambiguous = (
        len(ranked) > 1
        and ranked[0].kind == ranked[1].kind == "items"
        and ranked[0].score - ranked[1].score < AMBIGUOUS_MARGIN
        and ranked[0].score >= 0.2
        and bool(text.strip())
    )
    return {"suggestions": ranked, "ambiguous": ambiguous, "open_amount": open_amount}


# ------------------------------------------------------------------------------------------ model tie-break


def _candidate_text(index: int, s: Suggestion, currency: str) -> str:
    docs = "; ".join(
        f"{i.move_id.name} (ref {i.move_id.ref or i.move_id.payment_reference or '-'}, "
        f"partner {i.partner_id.elevated().name or '-'}, due {i.date_maturity or i.date}, "
        f"open {abs(float(i.amount_residual)):.2f} {currency})"
        for i in s.items
    )
    return f"[{index}] {docs} — total {abs(s.amount):.2f} {currency}"


def tie_break(env: Environment, st_line: Model, ranked: list[Suggestion]) -> tuple[int | None, Any]:
    """Ask the model which of the offered candidates the label means (index or None)."""
    from tessera.ai.runtime import run_feature

    line = st_line.elevated()
    currency = line.currency_id.name if line.currency_id else ""
    offered = ranked[:3]
    tx = (
        f"Date: {line.date}\nLabel: {line.payment_ref or ''}\nBank reference: {line.ref or ''}\n"
        f"Counterparty: {line.partner_name or ''}\nDetails: {line.narration or ''}\n"
        f"Amount: {abs(float(line.amount_residual or line.amount)):.2f} {currency}"
    )
    sources = [Source(f"account.bank_statement_line:{line.ids[0]}", line.display_name or "", tx)]
    candidates = "\n".join(_candidate_text(i, s, currency) for i, s in enumerate(offered))
    sources.append(Source(f"account.bank_statement_line:{line.ids[0]}:candidates", "Candidates", candidates))
    result = run_feature(env, FEATURE, {"candidates": len(offered)}, record=st_line, sources=sources)
    data = result.data if isinstance(result.data, dict) else {}
    choice = data.get("choice")
    if isinstance(choice, int) and not isinstance(choice, bool) and 0 <= choice < len(offered):
        return choice, result
    return None, result


# ------------------------------------------------------------------------------------------ the feature


def _check_line(st_line: Model) -> None:
    st_line.check_access("read")
    st_line.env["account.move_line"].check_access_rights("read")


def suggest(env: Environment, st_line: Model, *, limit: int = 5, propose: bool = True) -> dict[str, Any]:
    """Ranked suggestions for one transaction, the model's tie-break when ambiguous, and one ``action``
    proposal for the best candidate (nothing is matched before the user accepts it)."""
    ensure_enabled(env, FEATURE)
    _check_line(st_line)
    started = time.monotonic()
    ranked_info = rank(st_line, limit)
    ranked: list[Suggestion] = ranked_info["suggestions"]
    used_model = False
    model_run = None
    flags: list[str] = []
    if ranked_info["ambiguous"]:
        try:
            choice, model_run = tie_break(env, st_line, ranked)
            used_model = True
            if choice is None:
                flags.append("model_choice_rejected")
            elif choice:
                ranked.insert(0, ranked.pop(choice))
                ranked[0].reasons.append("model_choice")
        except AIError as exc:
            flags.append(exc.code)
    rows = [s.to_dict(i) for i, s in enumerate(ranked)]
    run = (
        model_run.run
        if model_run is not None
        else audit_run(
            env,
            FEATURE,
            record=st_line,
            inputs={"limit": limit},
            output=[{k: r[k] for k in ("line_ids", "score", "reasons")} for r in rows],
            model="feature ranking (rules + similarity + history)",
            started=started,
        )
    )
    proposal = None
    best = ranked[0] if ranked else None
    if propose and best is not None and best.score >= PROPOSE_MIN and _can_write(st_line):
        proposal = _propose(env, run, st_line, best)
    return {
        "run_id": run.ids[0],
        "line_id": st_line.ids[0],
        "suggestions": rows,
        "ambiguous": ranked_info["ambiguous"],
        "used_model": used_model,
        "flags": flags,
        "proposal": proposal,
        "ai_generated": True,
    }


def _can_write(st_line: Model) -> bool:
    try:
        st_line.check_access("write")
    except AccessError:
        return False
    return st_line.elevated().state == "posted"


def _propose(env: Environment, run: Model, st_line: Model, best: Suggestion) -> dict[str, Any]:
    from tessera.ai import proposals as P
    from tessera.ai.runtime import proposal_dict

    names = ", ".join(best.items.mapped("move_id").mapped("name")) if best.kind == "items" else best.model_name
    args = {
        "items": [{"line_id": i} for i in best.items.ids] if best.kind == "items" else [],
        "writeoffs": best.writeoffs,
    }
    title = f"Match {st_line.elevated().display_name} with {names}"
    p = P.create(
        env,
        run,
        FEATURE,
        PendingProposal(
            "action",
            "account.bank_statement_line",
            st_line.ids[0],
            {"action": ACTION, "args": args, "score": best.score, "reasons": best.reasons},
            title,
        ),
    )
    run.elevated().write({"decision": "pending"})
    return proposal_dict(p)


@action(ACTION)
def apply_match(env: Environment, model: str, res_id: int | None, args: dict[str, Any]) -> Any:
    """Accepting a proposal: match as the accepting user (write access on the transaction)."""
    from modules.account_bank.matching import match_line

    line = env["account.bank_statement_line"].browse([int(res_id or 0)]).exists()
    if not line:
        raise MissingError(_("The transaction no longer exists"))
    line.check_access("write")
    return match_line(line, list(args.get("items") or []), list(args.get("writeoffs") or []))


def suggest_many(env: Environment, lines: Model, limit: int = 100) -> dict[str, Any]:
    """The remainder after the rules: one proposal per transaction with a confident suggestion."""
    ensure_enabled(env, FEATURE)
    rows = []
    for line in lines[:limit]:
        try:
            out = suggest(env, line, limit=3)
        except (AccessError, MissingError, UserError) as exc:
            rows.append({"line_id": line.ids[0], "error": exc.message})
            continue
        best = out["suggestions"][0] if out["suggestions"] else None
        rows.append(
            {
                "line_id": line.ids[0],
                "label": line.elevated().payment_ref,
                "best": best,
                "proposal": out["proposal"],
                "run_id": out["run_id"],
            }
        )
    return {"rows": rows, "proposed": sum(1 for r in rows if r.get("proposal")), "ai_generated": True}


# ------------------------------------------------------------------------------------------ API

router = register_router(APIRouter(prefix="/account_bank/ai", tags=["account_bank", "ai"]))


def _line(ctx: RequestContext, line_id: int) -> Model:
    line = ctx.env["account.bank_statement_line"].browse([line_id])
    if not line.exists():
        raise MissingError(_("No such bank transaction"))
    _check_line(line)
    return line


@router.post("/lines/{line_id}/suggest")
def suggest_line(line_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Ranked suggestions for a transaction and a match proposal for the best one."""
    ensure_enabled(ctx.env, FEATURE)
    return suggest(ctx.env, _line(ctx, line_id))


class ManyIn(BaseModel):
    journal_id: int | None = None
    line_ids: list[int] | None = Field(None, max_length=200)


@router.post("/suggest")
def suggest_open(data: ManyIn, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Proposals for the transactions the rules left open (the user's companies, one journal or given ids)."""
    ensure_enabled(ctx.env, FEATURE)
    lines = ctx.env["account.bank_statement_line"]
    if data.line_ids:
        lines = lines.browse(data.line_ids).exists()
    else:
        domain: list[Any] = [("state", "=", "posted")]
        if data.journal_id:
            domain.append(("journal_id", "=", data.journal_id))
        lines = lines.search(domain, order="date, id", limit=100)
    lines.check_access("read")
    return suggest_many(ctx.env, lines)


@router.get("/enabled")
def enabled(ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    return {"enabled": feature_enabled(ctx.env, FEATURE)}
