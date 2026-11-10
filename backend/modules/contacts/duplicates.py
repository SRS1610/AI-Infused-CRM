"""Duplicate detection, fuzzy + semantic (feature ``contacts.duplicates``).

Candidates come from three places, all searched **as the requesting user** (record rules and the
multi-company rule apply, so a contact the user cannot read is never a candidate):

* the rule-based :meth:`Partner.find_duplicates` (same e-mail, phone, or name in the same company),
* trigram similarity on the name (the ``pg_trgm`` index on ``contacts_partner.name``) and shared
  e-mail / website domains or tax ids,
* every candidate is then compared semantically: the profile texts are embedded through the AI
  gateway (``contacts.duplicates`` route) and their cosine similarity is one more signal.

A small, deterministic classifier (a logistic score over the signals, below) decides which
candidates are duplicates and explains why. Nothing is written: merging is a proposal of kind
``action`` that runs :func:`modules.contacts.merge.merge_partners` as the user who accepts it.

Calibration: the weights were set by hand against the labelled set in
``ai/evals/contacts.duplicates.jsonl`` (``ai/build_evals.py``), which the evaluation harness scores
for precision and recall. In replay/CI the embeddings are the gateway's deterministic bag-of-words
hash embeddings, so the semantic signal there is lexical; with a real embedding model it is
semantic and the weight (``W_EMBED``) is deliberately small.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from tessera.ai import proposals as P
from tessera.ai.errors import AIError
from tessera.ai.tools import PendingProposal
from tessera.exceptions import UserError

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "contacts.duplicates"
THRESHOLD = 0.5
MAX_CANDIDATES = 40
PUBLIC_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.de", "gmx.net", "web.de", "orange.fr",
    "free.fr", "laposte.net", "mail.example", "example.com", "example.org", "mail.test", "gmail.test",
}  # fmt: skip
LEGAL_WORDS = {
    "inc", "incorporated", "ltd", "limited", "llc", "llp", "gmbh", "ag", "sa", "sas", "sarl", "srl", "spa",
    "bv", "nv", "plc", "co", "corp", "corporation", "company", "pty", "oy", "ab", "kg", "the", "and",
}  # fmt: skip
ABBREVIATIONS = {
    "intl": "international", "int": "international", "tech": "technology", "technologies": "technology",
    "svc": "services", "svcs": "services", "service": "services", "mfg": "manufacturing", "bros": "brothers",
    "assoc": "associates", "dept": "department", "univ": "university", "labs": "lab", "laboratories": "lab",
    "systems": "system", "solutions": "solution",
}  # fmt: skip

# classifier weights (logistic regression form, hand-calibrated; see module docstring)
W_BIAS = -4.0
W_EMAIL = 5.0
W_EMAIL_OTHER_NAME = -3.0  # one mailbox, clearly different names: a shared address (info@, a team inbox)
W_PHONE = 3.0
W_PHONE_OTHER_NAME = -1.5  # a shared switchboard number with a clearly different name: colleagues
W_VAT = 5.0
W_VAT_CONFLICT = -3.0
W_NAME = 7.0  # times the part of the name similarity above NAME_FLOOR, scaled to 0..1
NAME_FLOOR = 0.55
W_DOMAIN = 1.5
W_DOMAIN_CONFLICT = -3.0  # both have a business domain and they differ: different employers / companies
W_CITY = 0.6
W_CITY_CONFLICT = -1.0
W_GIVEN_NAME_CONFLICT = -3.0  # people sharing a surname but not a first name (family, colleagues)
W_PARENT_CONFLICT = -3.5
W_KIND_CONFLICT = -1.5
W_EMBED = 2.0  # times the part of the cosine above 0.5, scaled to 0..1


def fold(text: str | None) -> str:
    """Lower case without accents."""
    norm = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def name_tokens(name: str | None) -> list[str]:
    words = re.findall(r"[a-z0-9]+", fold(name).replace("&", " and "))
    out = [ABBREVIATIONS.get(w, w) for w in words]
    return [w for w in out if w not in LEGAL_WORDS]


def _token_match(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) == 1 or len(b) == 1:  # an initial: "J." matches "Jordan"
        return 0.85 if a[0] == b[0] else 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def given_name_conflict(a: str | None, b: str | None) -> bool:
    """Two people's names share a token but another token has no plausible counterpart
    ("Anna Sorensen" / "Erik Sorensen"); initials and typos are counterparts ("J." / "Jordan")."""
    ta, tb = name_tokens(a), name_tokens(b)
    if len(ta) < 2 or len(tb) < 2 or not set(ta) & set(tb):
        return False
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return any(max(_token_match(t, u) for u in long_) < 0.6 for t in short)


def name_similarity(a: str | None, b: str | None) -> float:
    """0..1, insensitive to case, accents, legal suffixes, common abbreviations, word order and initials."""
    ta, tb = name_tokens(a), name_tokens(b)
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    ordered = difflib.SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    unordered = difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    soft = sum(max(_token_match(t, u) for u in long_) for t in short) / len(long_)
    return round(max(jaccard, ordered, unordered, soft), 4)


def email_domain(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.rsplit("@", 1)[1].strip().lower() or None


def website_domain(url: str | None) -> str | None:
    if not url:
        return None
    host = re.sub(r"^[a-z]+://", "", url.strip().lower()).split("/", 1)[0].split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host or None


def business_domains(p: Model) -> set[str]:
    out = {d for d in (email_domain(p.email_normalized), website_domain(p.website)) if d}
    return {d for d in out if d not in PUBLIC_DOMAINS}


def profile_text(p: Model) -> str:
    """What is embedded for the semantic comparison (fields the user can read)."""
    parts = [
        p.name or "",
        p.parent_id.name if p.parent_id else "",
        p.function or "",
        p.email_normalized or "",
        website_domain(p.website) or "",
        p.city or "",
        p.street or "",
        p.country_id.name if p.country_id else "",
        p.vat or "",
    ]
    return " ".join(x for x in parts if x)


@dataclass
class Candidate:
    record: Model
    signals: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def duplicate(self) -> bool:
        return self.score >= THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        r = self.record
        return {
            "id": r.ids[0],
            "display_name": r.display_name,
            "email": r.email,
            "phone": r.phone or r.mobile,
            "city": r.city,
            "is_company": r.is_company,
            "score": round(self.score, 4),
            "duplicate": self.duplicate,
            "reasons": self.reasons,
            "signals": self.signals,
        }


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def _commercial(p: Model) -> int | None:
    if p.is_company or not p.parent_id:
        return None
    entity = p.parent_id.commercial_partner_id or p.parent_id
    return entity.ids[0] if entity else None


def score_pair(me: Model, other: Model, embed_cos: float | None) -> Candidate:
    """Signals, score and human-readable reasons for one pair (both readable by the user)."""
    c = Candidate(other)
    s = c.signals
    reasons = c.reasons
    z = W_BIAS
    nsim = name_similarity(me.name, other.name)
    s["name_similarity"] = nsim
    z += W_NAME * min(1.0, max(0.0, (nsim - NAME_FLOOR) / (1 - NAME_FLOOR)))
    if nsim >= 0.999:
        reasons.append("Same name")
    elif nsim >= 0.8:
        reasons.append(f"Very similar name ({round(nsim * 100)}%)")
    elif nsim >= 0.65:
        reasons.append(f"Similar name ({round(nsim * 100)}%)")
    if me.email_normalized and me.email_normalized == other.email_normalized:
        s["email"] = True
        z += W_EMAIL + (W_EMAIL_OTHER_NAME if nsim < 0.4 else 0.0)
        reasons.append("Same e-mail address")
    if not me.is_company and not other.is_company and given_name_conflict(me.name, other.name):
        s["given_name_conflict"] = True
        z += W_GIVEN_NAME_CONFLICT
    if me.phone_normalized and me.phone_normalized == other.phone_normalized:
        s["phone"] = True
        z += W_PHONE + (W_PHONE_OTHER_NAME if nsim < 0.5 else 0.0)
        reasons.append("Same phone number")
    vat_a, vat_b = (re.sub(r"\W", "", (x or "").upper()) for x in (me.vat, other.vat))
    if vat_a and vat_b:
        if vat_a == vat_b:
            s["vat"] = True
            z += W_VAT
            reasons.append("Same tax ID")
        else:
            s["vat_conflict"] = True
            z += W_VAT_CONFLICT
    da, db = business_domains(me), business_domains(other)
    if da and db:
        shared = sorted(da & db)
        if shared:
            s["domain"] = shared[0]
            z += W_DOMAIN
            if not s.get("email"):
                reasons.append(f"Same domain ({shared[0]})")
        else:
            s["domain_conflict"] = True
            z += W_DOMAIN_CONFLICT
    ca, cb = fold(me.city).strip(), fold(other.city).strip()
    if ca and cb:
        if ca == cb:
            s["city"] = True
            z += W_CITY
        else:
            s["city_conflict"] = True
            z += W_CITY_CONFLICT
    pa, pb = _commercial(me), _commercial(other)
    if pa and pb and pa != pb:
        s["parent_conflict"] = True
        z += W_PARENT_CONFLICT
    elif pa and pa == pb:
        s["same_company"] = True
    if bool(me.is_company) != bool(other.is_company):
        s["kind_conflict"] = True
        z += W_KIND_CONFLICT
    if embed_cos is not None:
        s["semantic_similarity"] = round(embed_cos, 4)
        z += W_EMBED * min(1.0, max(0.0, (embed_cos - 0.5) / 0.5))
        if embed_cos >= 0.75 and nsim < 0.8:
            reasons.append(f"Similar profile (semantic match {round(embed_cos * 100)}%)")
    if s.get("same_company") and nsim >= 0.65:
        reasons.append("Same company")
    if s.get("city") and reasons:
        reasons.append("Same city")
    c.score = 1 / (1 + math.exp(-z))
    return c


def candidate_records(me: Model, limit: int = MAX_CANDIDATES) -> Model:
    """Contacts the user can read that might be the same as ``me`` (``me``'s env is the user's)."""
    env = me.env
    partners = env["contacts.partner"]
    ids: dict[int, None] = {}
    for row in me.find_duplicates(limit=limit):
        ids[row["id"]] = None
    name = (me.name or "").strip()
    if name:
        # pg_trgm on the indexed name column; the ORM search below re-applies the user's record rules
        rows = env.cr.fetchall(
            "SELECT id FROM contacts_partner WHERE active AND id != :id AND name IS NOT NULL "
            "AND similarity(name, :n) > 0.3 ORDER BY similarity(name, :n) DESC LIMIT :lim",
            {"id": me.ids[0], "n": name, "lim": limit},
        )
        ids.update({r[0]: None for r in rows})
        tokens = sorted({t for t in name_tokens(name) if len(t) >= 4}, key=len, reverse=True)[:3]
        if tokens:
            domain: list[Any] = [{"any": [["name", "ilike", t] for t in tokens]}, ("id", "!=", me.ids[0])]
            ids.update(dict.fromkeys(partners.search(domain, limit=limit).ids))
    for dom in business_domains(me):
        found = partners.search(
            [{"any": [["email_normalized", "like", f"%@{dom}"], ["website", "ilike", dom]]}, ("id", "!=", me.ids[0])],
            limit=limit,
        )
        ids.update(dict.fromkeys(found.ids))
    if me.vat:
        ids.update(dict.fromkeys(partners.search([("vat", "=", me.vat), ("id", "!=", me.ids[0])], limit=10).ids))
    ids.pop(me.ids[0], None)
    # as the user: anything their record rules hide is dropped here, before scoring
    return partners.search([("id", "in", list(ids))], limit=limit * 2) if ids else partners


def _embeddings(env: Environment, texts: list[str]) -> list[list[float]] | None:
    from tessera.ai.client import CallContext, get_client

    try:
        resp = get_client(env).embed(
            CallContext(env.company.ids[0] if env.company else 0, env.uid, FEATURE), texts, "document"
        )
    except AIError:
        return None  # the fuzzy signals still work without the gateway
    vectors: list[list[float]] = resp["vectors"]
    return vectors


def detect(partner: Model, *, limit: int = 10, semantic: bool = True) -> dict[str, Any]:
    """Ranked duplicate candidates of ``partner`` with reasons (``partner.env`` is the user's)."""
    partner.ensure_one()
    partner.check_access("read")
    candidates = candidate_records(partner)
    cos: dict[int, float] = {}
    embedding = "off"
    if semantic and candidates:
        vectors = _embeddings(partner.env, [profile_text(partner), *[profile_text(c) for c in candidates]])
        if vectors is None:
            embedding = "unavailable"
        else:
            embedding = "ok"
            cos = {c.ids[0]: _cosine(vectors[0], v) for c, v in zip(candidates, vectors[1:], strict=True)}
    scored = [score_pair(partner, c, cos.get(c.ids[0])) for c in candidates]
    scored.sort(key=lambda c: (-c.score, c.record.ids[0]))
    shown = [c for c in scored if c.duplicate or c.score >= 0.2][:limit]
    return {
        "partner_id": partner.ids[0],
        "threshold": THRESHOLD,
        "embedding": embedding,
        "candidates": [c.to_dict() for c in shown],
        "duplicates": [c.record.ids[0] for c in scored if c.duplicate],
    }


# -- merge as a proposal ---------------------------------------------------------------------------


def propose_merge(env: Environment, partner: Model, other_ids: list[int], run: Model | None = None) -> Model:
    """A proposal of kind ``action`` that merges ``other_ids`` and ``partner`` into the oldest of them."""
    others = env["contacts.partner"].browse([i for i in dict.fromkeys(other_ids) if i != partner.ids[0]])
    if not others:
        raise UserError("Choose at least one other contact to merge")
    group = partner | others
    group.check_access("write")  # proposing a merge the user could not make is pointless
    survivor = group.sorted(lambda r: r.ids[0])[:1]
    merged = group - survivor
    title = f"Merge {', '.join(merged.mapped('display_name'))} into {survivor.display_name}"
    return P.create(
        env,
        run,
        FEATURE,
        PendingProposal(
            "action",
            "contacts.partner",
            survivor.ids[0],
            {"action": "contacts.merge", "args": {"merge_ids": merged.ids}},
            title,
        ),
    )


@P.action("contacts.merge")
def merge_action(env: Environment, model: str, res_id: int | None, args: dict[str, Any]) -> Any:
    """Run the existing merge as the accepting user (their write rights are checked again)."""
    from modules.contacts.merge import merge_partners

    if model != "contacts.partner" or not res_id:
        raise UserError("A merge needs the contact to keep")
    survivor = env["contacts.partner"].browse([res_id])
    others = env["contacts.partner"].browse([int(i) for i in args.get("merge_ids") or []])
    if not survivor.exists() or len(others.exists()) != len(others):
        raise UserError("One of the contacts to merge no longer exists")
    log = merge_partners(env, survivor, others, dict(args.get("field_choices") or {}))
    log.elevated().write({"method": "ai_suggested"})
    return log
