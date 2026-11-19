"""Anomaly detection on vendor bills (``account.anomaly``): notification / flag, never a block.

The detector (:func:`detect`) is a pure function of the bill's facts, the partner's other documents and
what the company knows of the partner; it finds

* **duplicates**: the same invoice number (also when written differently or misread by OCR), the same
  amount within a few days, or a nearly identical document text (word-shingle similarity) with the same
  amount;
* **unusual amounts** for the vendor: robust z-score on its history (median / MAD, with a relative floor
  so a vendor with near-constant amounts is not flagged for cents);
* **unexpected vendors**: the first bill of a vendor above the company's usual large amount (90th
  percentile of its bills), and a vendor whose bank account was added or changed recently, or whose bill
  shows an IBAN that is not on file.

:func:`check_move` stores the findings as ``account.move_anomaly`` flags with their explanation and
notifies the document's responsible; it runs as a background job after a bill is created or posted, on
demand from the bill, and on the reading of a document before it becomes a bill. Posting is never
blocked. Everything reads as the requesting user, except the job, which acts for the bill's company.
"""

from __future__ import annotations

import datetime as dt
import re
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from modules.account import ai_ocr as O
from tessera.ai import Source
from tessera.ai.errors import AIError
from tessera.ai.runtime import check_enabled, get_feature, handler
from tessera.exceptions import AccessError, MissingError
from tessera.i18n import _
from tessera.jobs import enqueue, job
from tessera.logging import get_logger
from tessera.orm.hooks import hook

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)
FEATURE = "account.anomaly"
PURCHASE = ("in_invoice", "in_refund")
DUPLICATE_KINDS = ("duplicate_number", "duplicate_amount", "near_duplicate_text")
DUP_DAYS = 7
TEXT_DAYS = 10
Z_LIMIT = 3.5
REL_FLOOR = 0.5
Z_SOFT = 2.0  # a softer z-score is enough when the amount is under a fifth of the median
LOW_SHARE = 0.2
MIN_HISTORY = 4
BANK_RECENT_DAYS = 30
NEW_VENDOR_FLOOR = 1000.0
NEW_VENDOR_DEFAULT = 5000.0


@dataclass
class Bill:
    """What the detector knows of one document."""

    id: int | None
    partner: str  # commercial partner id (as text) or normalised name
    number: str
    date: dt.date
    amount: float  # total in company currency, positive
    text: str = ""
    label: str = ""

    @property
    def number_key(self) -> str:
        return number_key(self.number)


@dataclass
class Vendor:
    """What the company knows of the partner, at the bill's date."""

    known_ibans: list[str] = field(default_factory=list)
    bank_changed_days: int | None = None  # days since a bank account was added / changed (None: never)
    has_older_bills: bool = False
    document_iban: str | None = None


@dataclass
class Finding:
    kind: str
    severity: str
    score: float
    explanation: str
    related_id: int | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "score": round(self.score, 3),
            "explanation": self.explanation,
            "related_id": self.related_id,
            "details": self.details,
        }


def _money(v: float) -> str:
    return f"{v:,.2f}"


def shingles(text: str, n: int = 3) -> set[str]:
    w = O.words(text)
    return {" ".join(w[i : i + n]) for i in range(max(0, len(w) - n + 1))}


def text_similarity(a: str, b: str) -> float:
    sa, sb = shingles(a), shingles(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


CONFUSABLE = str.maketrans({"O": "0", "D": "0", "Q": "0", "I": "1", "L": "1", "S": "5", "B": "8", "Z": "2", "G": "6"})


def number_key(number: str) -> str:
    """An invoice number without separators and leading zeros (``INV-0042`` = ``inv 42`` = ``INV/42``)."""
    return re.sub(r"(?<!\d)0+(?=\d)", "", re.sub(r"[^A-Z0-9]", "", (number or "").upper()))


def ocr_key(number: str) -> str:
    """An invoice number with the characters scans confuse folded together (``INV-1O01`` = ``INV1001``)."""
    return number_key(re.sub(r"[^A-Z0-9]", "", (number or "").upper()).translate(CONFUSABLE))


def robust_z(value: float, history: list[float]) -> tuple[float, float]:
    """(robust z-score, median) of ``value`` against ``history`` (MAD scaled to sigma)."""
    med = statistics.median(history)
    mad = statistics.median([abs(h - med) for h in history])
    if mad == 0:
        spread = max(abs(med) * 0.05, 0.01)  # near-constant history: 5 % of the median as one sigma
        return (value - med) / spread, med
    return 0.6745 * (value - med) / mad, med


def detect(bill: Bill, others: list[Bill], vendor: Vendor, company_amounts: list[float]) -> list[Finding]:
    """Findings for ``bill`` given the partner's other documents (``others``), what is known of the
    partner and the amounts of the company's recent bills (for the new-vendor threshold)."""
    out: list[Finding] = []
    same = [o for o in others if o.partner == bill.partner and o.id != bill.id]
    # -- duplicates ------------------------------------------------------------------------------------
    dup_found = False
    for o in same:
        if bill.number_key and o.number_key == bill.number_key:
            out.append(
                Finding(
                    "duplicate_number",
                    "high",
                    1.0,
                    _(
                        "Same invoice number {number} as {other} of {date} ({amount}).",
                        number=bill.number,
                        other=o.label or o.number,
                        date=o.date.isoformat(),
                        amount=_money(o.amount),
                    ),
                    o.id,
                    {"number": bill.number},
                )
            )
            dup_found = True
            break
    if not dup_found:
        for o in same:
            if bill.number and o.number and ocr_key(bill.number) == ocr_key(o.number):
                out.append(
                    Finding(
                        "duplicate_number",
                        "medium",
                        0.9,
                        _(
                            "Invoice number {number} reads like {onumber} of {other} (characters a scan confuses).",
                            number=bill.number,
                            onumber=o.number,
                            other=o.label or o.number,
                        ),
                        o.id,
                    )
                )
                dup_found = True
                break
    if not dup_found:
        for o in same:
            days = abs((o.date - bill.date).days)
            if abs(o.amount - bill.amount) <= 0.01 and days <= DUP_DAYS:
                out.append(
                    Finding(
                        "duplicate_amount",
                        "medium",
                        1.0 - days / (DUP_DAYS + 1),
                        _(
                            "Same amount {amount} as {other}, {days} days apart.",
                            amount=_money(bill.amount),
                            other=o.label or o.number,
                            days=days,
                        ),
                        o.id,
                    )
                )
                dup_found = True
                break
    if not dup_found and bill.text:
        for o in others:
            if o.id == bill.id or not o.text:
                continue
            sim = text_similarity(bill.text, o.text)
            days = abs((o.date - bill.date).days)
            same_amount = abs(o.amount - bill.amount) <= max(0.01, 0.01 * bill.amount)
            same_number = bool(bill.number) and ocr_key(bill.number) == ocr_key(o.number)
            if sim >= 0.85 and same_amount and (days <= TEXT_DAYS or same_number):
                out.append(
                    Finding(
                        "near_duplicate_text",
                        "high",
                        sim,
                        _(
                            "The document is {percent}% identical to {other} of {date}, same amount {amount}.",
                            percent=int(sim * 100),
                            other=o.label or o.number,
                            date=o.date.isoformat(),
                            amount=_money(o.amount),
                        ),
                        o.id,
                    )
                )
                break
    # -- unusual amount --------------------------------------------------------------------------------
    past = [o.amount for o in same if o.date <= bill.date]
    if len(past) >= MIN_HISTORY:
        z, med = robust_z(bill.amount, past)
        rel = abs(bill.amount - med) / med if med else 0.0
        if (abs(z) >= Z_LIMIT and rel >= REL_FLOOR) or (z <= -Z_SOFT and bill.amount <= LOW_SHARE * med):
            out.append(
                Finding(
                    "unusual_amount",
                    "high" if abs(z) >= 2 * Z_LIMIT else "medium",
                    z,
                    _(
                        "{amount} is {ratio} times the usual amount of this vendor "
                        "(median {median} over {count} bills).",
                        amount=_money(bill.amount),
                        ratio=f"{bill.amount / med:.1f}" if med else "∞",
                        median=_money(med),
                        count=len(past),
                    ),
                    details={"median": med, "z": round(z, 2), "history": len(past)},
                )
            )
    # -- unexpected vendor -----------------------------------------------------------------------------
    if not same and not vendor.has_older_bills:
        recent = sorted(company_amounts)
        threshold = NEW_VENDOR_DEFAULT
        if len(recent) >= 10:
            threshold = max(NEW_VENDOR_FLOOR, recent[int(0.9 * (len(recent) - 1))])
        if bill.amount >= threshold:
            out.append(
                Finding(
                    "new_vendor_large",
                    "medium",
                    bill.amount / threshold,
                    _(
                        "First bill of this vendor and above the company's usual large amount "
                        "({amount} ≥ {threshold}).",
                        amount=_money(bill.amount),
                        threshold=_money(threshold),
                    ),
                    details={"threshold": threshold},
                )
            )
    if vendor.bank_changed_days is not None and vendor.bank_changed_days <= BANK_RECENT_DAYS and vendor.has_older_bills:
        out.append(
            Finding(
                "bank_account_changed",
                "high",
                1.0,
                _(
                    "The vendor's bank account was added or changed {days} days ago: confirm it with the vendor "
                    "through a known contact before paying.",
                    days=vendor.bank_changed_days,
                ),
            )
        )
    elif vendor.document_iban and vendor.known_ibans and O.norm_iban(vendor.document_iban) not in vendor.known_ibans:
        out.append(
            Finding(
                "bank_account_changed",
                "high",
                1.0,
                _(
                    "The bill asks for payment to an IBAN ending in {tail}, which is not the bank account on file "
                    "for this vendor.",
                    tail=O.norm_iban(vendor.document_iban)[-4:],
                ),
                details={"iban_tail": O.norm_iban(vendor.document_iban)[-4:]},
            )
        )
    return out


# ----------------------------------------------------------------------------- the ledger as facts


def feature_enabled(env: Environment) -> bool:
    if "ai_core.feature" not in env.registry.models:
        return False
    try:
        check_enabled(env, get_feature(env, FEATURE))
    except (AIError, AccessError, MissingError):
        return False
    return True


def _partner_key(move: Model) -> str:
    p = move.commercial_partner_id or move.partner_id
    return str(p.ids[0]) if p else ""


def bill_facts(move: Model) -> Bill:
    text = move.ai_ocr_text or ""
    if not text:
        text = " ".join([move.ref or "", *[ln.name or "" for ln in move.invoice_line_ids]])
    return Bill(
        move.ids[0] if move.ids else None,
        _partner_key(move),
        move.ref or "",
        move.invoice_date or move.date,
        abs(float(move.amount_total_signed or move.amount_total or 0.0)),
        text,
        move.display_name or "",
    )


def partner_bills(env: Environment, partner_id: int | None, company_id: int, exclude: int | None = None) -> list[Bill]:
    if not partner_id:
        return []
    moves = env["account.move"].search(
        [
            ("move_type", "in", list(PURCHASE)),
            ("commercial_partner_id", "=", partner_id),
            ("state", "!=", "cancel"),
            ("company_id", "=", company_id),
            ("id", "!=", exclude or 0),
        ],
        order="invoice_date desc, id desc",
        limit=300,
    )
    return [bill_facts(m) for m in moves]


def company_amounts(env: Environment, company_id: int, day: dt.date) -> list[float]:
    moves = env["account.move"].search(
        [
            ("move_type", "=", "in_invoice"),
            ("state", "=", "posted"),
            ("company_id", "=", company_id),
            ("invoice_date", ">=", day - dt.timedelta(days=365)),
        ],
        limit=2000,
    )
    return [abs(float(m.amount_total_signed or 0.0)) for m in moves]


def vendor_facts(env: Environment, partner_id: int | None, day: dt.date, document_iban: str | None = None) -> Vendor:
    if not partner_id:
        return Vendor(document_iban=document_iban)
    partner = env["contacts.partner"].browse([partner_id])
    banks = env["account.bank_account"].search([("partner_id", "child_of", partner_id)])
    ibans = [O.norm_iban(b.acc_number) for b in banks]
    changed = None
    oldest_bill = env["account.move"].search(
        [("move_type", "in", list(PURCHASE)), ("commercial_partner_id", "=", partner.ids[0]), ("state", "=", "posted")],
        order="invoice_date asc",
        limit=1,
    )
    for b in banks:
        when = b.elevated().write_date or b.elevated().create_date
        if when is not None:
            days = (day - when.date()).days
            if oldest_bill and oldest_bill.invoice_date and oldest_bill.invoice_date < when.date():
                changed = days if changed is None else min(changed, days)
    return Vendor(ibans, changed if changed is not None and changed >= 0 else None, bool(oldest_bill), document_iban)


def detect_for(
    env: Environment, bill: Bill, partner_id: int | None, company_id: int, iban: str | None = None
) -> list[Finding]:
    others = partner_bills(env, partner_id, company_id, exclude=bill.id)
    vendor = vendor_facts(env, partner_id, bill.date, iban)
    return detect(bill, others, vendor, company_amounts(env, company_id, bill.date))


def check_move(env: Environment, move: Model, *, notify: bool = True) -> list[Model]:
    """Detect and store the flags of one bill (as ``env``'s user for the reads); returns the new flags."""
    move = move.with_company(move.company_id) if move.company_id else move
    env = move.env
    if move.move_type not in PURCHASE or move.state == "cancel":
        return []
    bill = bill_facts(move)
    partner = move.commercial_partner_id or move.partner_id
    found = detect_for(env, bill, partner.ids[0] if partner else None, move.company_id.ids[0])
    flags = env["account.move_anomaly"].elevated("anomaly flags")
    existing = flags.search([("move_id", "=", move.ids[0])])
    new: list[Model] = []
    keep: set[int] = set()
    for f in found:
        same = existing.filtered(
            lambda e, f=f: e.kind == f.kind and (e.related_move_id.ids[:1] or [None])[0] == f.related_id
        )
        if same:
            keep.update(same.ids)
            continue
        new.append(
            flags.create(
                {
                    "move_id": move.ids[0],
                    "kind": f.kind,
                    "severity": f.severity,
                    "score": f.score,
                    "explanation": f.explanation,
                    "related_move_id": f.related_id,
                    "details": f.details,
                }
            )
        )
    stale = existing.filtered(lambda e: e.state == "open" and e.ids[0] not in keep)
    if stale:
        stale.unlink()
    if new and notify:
        user = move.elevated().invoice_user_id or move.elevated().create_uid
        if user:
            env["core.notification"].notify(
                user.ids,
                _("Check bill {name}", name=move.display_name),
                "; ".join(f.explanation for f in new)[:500],
                kind="system",
                model="account.move",
                res_id=move.ids[0],
                url=f"/m/account.move/{move.ids[0]}",
            )
    return new


@job("account.check_anomalies", queue="ai", max_attempts=2, backoff_seconds=60)
def check_anomalies_job(env: Environment, move_id: int) -> int:
    """Background check after a bill is created or posted (acts for the bill's company)."""
    move = env["account.move"].elevated("anomaly check").browse([move_id]).exists()
    if not move or not feature_enabled(env.with_company(move.company_id)):
        return 0
    return len(check_move(env.elevated("anomaly check").with_company(move.company_id), move))


def _queue(records: Model) -> None:
    env = records.env
    if env.context.get("skip_ai_anomaly") or len(records) > 20 or "core.job" not in env.registry.models:
        return
    for move in records:
        if move.move_type in PURCHASE:
            enqueue(env, "account.check_anomalies", {"move_id": move.ids[0]}, description="Anomaly check")


@hook("after_create")
def queue_on_create(records: Model, vals_list: list[dict[str, Any]]) -> None:
    if records._name == "account.move" and "ai_core.feature" in records.env.registry.models:
        _queue(records.filtered(lambda m: m.move_type in PURCHASE))


@hook("after_write")
def queue_on_post(records: Model, vals: dict[str, Any], old_values: Any) -> None:
    if records._name == "account.move" and vals.get("state") == "posted":
        _queue(records.filtered(lambda m: m.move_type in PURCHASE))


# ----------------------------------------------------------------------------- explanation (model)


def flags_text(move: Model) -> str:
    lines = [
        f"Document: {move.display_name}",
        f"Vendor: {move.partner_id.display_name if move.partner_id else ''}",
        f"Invoice number: {move.ref or ''}",
        f"Date: {move.invoice_date or move.date}",
        f"Total: {_money(float(move.amount_total or 0))} {move.currency_id.name if move.currency_id else ''}",
        "Flags:",
    ]
    for flag in move.ai_anomaly_ids.filtered(lambda f: f.state == "open"):
        lines.append(f"- {flag.kind}: {flag.explanation}")
    return "\n".join(lines)


@handler(FEATURE)
def explain_context(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    """Plain-words summary of a bill's open flags (the detector decides; the model only explains, and
    an answer quoting a number that is not in the flags is replaced by the flags themselves)."""
    if record is None or record._name != "account.move":
        raise MissingError(_("Open a bill to explain its flags"))
    record.check_access("read")
    facts = flags_text(record)
    sources = [Source(f"account.move:{record.ids[0]}", record.display_name or "", facts)]
    for flag in record.ai_anomaly_ids.filtered(lambda f: f.state == "open" and f.related_move_id):
        try:
            other = flag.related_move_id
            other.check_access("read")
        except AccessError:
            continue
        sources.append(Source(f"account.move:{other.ids[0]}", other.display_name or "", flags_text(other)))
    from modules.account.ai_cashflow import numbers_guard

    return {"sources": sources, "on_output": numbers_guard("\n".join(s.text for s in sources), facts)}
