"""``crm.followup_draft`` — follow-up and proposal e-mails, streamed into the lead's chatter composer.

Sources (read as the requesting user): the lead with its latest discussion, its quotations line by line,
and catalogue products the conversation mentions. The draft becomes a **message proposal**; nothing is
sent until the user sends it from the composer (``proposal.accept``).

Prices: an amount may only come from a quotation or a product source. Every currency amount in the
draft, and every number written without a currency that looks like a price (``the total is 12,500``,
``a discount of 2,000``; not dates, times, quantities, phone numbers, percentages or identifiers), is
checked against the numbers in those sources (never against the lead's internal revenue estimate);
amounts that do not appear there are listed on the proposal as ``unverified_amounts`` and counted by the
evaluation (``price_grounding`` must be 1.0 — zero fabricated prices; ``price_check_accuracy`` measures
the check itself on probe drafts).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from modules.crm.ai_assist import LEAD, lead_source, money, readable_lead
from tessera.ai import Source
from tessera.ai.runtime import HANDLERS, RunResult, handler, run_feature
from tessera.ai.tools import PendingProposal, ToolContext

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "crm.followup_draft"
NUM = r"\d{1,3}(?:[,.\u00a0\u202f' ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
CUR = r"[$€£¥₹]|\b(?:USD|EUR|GBP|CHF|JPY|INR|AUD|CAD|SGD)\b"
CUR_AFTER = CUR + r"|\b(?:euros?|dollars?|pounds?)\b"
SCALE = r"\s?(?:k|K|m|M|mn|million|thousand)\b"
AMOUNT = re.compile(
    rf"(?:(?:{CUR})\s?(?P<n1>{NUM})(?P<s1>{SCALE})?)|(?:(?P<n2>{NUM})(?P<s2>{SCALE})?\s?(?:{CUR_AFTER}))",
    re.IGNORECASE,
)
ANY_NUMBER = re.compile(NUM)


def parse_number(raw: str) -> float | None:
    """``18,900.00`` / ``18.900,00`` / ``18 900`` / ``113.4`` → float."""
    s = re.sub(r"[\u00a0\u202f' ]", "", raw)
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        s = s.replace(sep, ".") if len(parts) == 2 and len(parts[1]) in (1, 2) else s.replace(sep, "")
    try:
        return float(s)
    except ValueError:
        return None


def _scaled(value: float, suffix: str | None) -> float:
    sx = (suffix or "").strip().lower()
    if sx in ("k", "thousand"):
        return value * 1_000
    if sx in ("m", "mn", "million"):
        return value * 1_000_000
    return value


def amounts_in(text: str) -> list[tuple[str, float]]:
    """Currency amounts written in a draft: (as written, value)."""
    out = []
    for m in AMOUNT.finditer(text or ""):
        raw = m.group("n1") or m.group("n2")
        value = parse_number(raw)
        if value is not None:
            out.append((m.group(0).strip(), _scaled(value, m.group("s1") or m.group("s2"))))
    return out


# -- bare amounts: a price typed without a currency ("the total is 12,500") ---------------------------------
# A number counts as a price when it is written like one (thousands separators, or exactly two decimals) or
# when a pricing word comes just before it ("total", "price", "discount of" …). Dates, times, phone numbers,
# percentages, years, quantities ("6 arms", "1,200 parts"), identifiers ("line 2", "#4500") and numbers glued
# to letters ("HW-ARM6", "30-minute") are not prices.
BARE = re.compile(
    r"(?<![\w#@/:.,'\u00a0\u202f-])"
    r"(?P<num>\d{1,3}(?:(?P<sep>[,.\u00a0\u202f' ])\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"(?P<scale>\s?(?:k|m|mn|million|thousand)\b)?"
    r"(?![\w%/:@]|-[^\W\d]|[.,]\d)",
    re.IGNORECASE,
)
PRICE_WORDS = (
    r"total|totals|subtotal|sub-total|price|prices|priced|pricing|cost|costs|costing|fee|fees|rate|rates|amount|"
    r"discount|discounted|charge|charges|charged|invoice|invoiced|pay|paying|payment|payable|budget|quote|quoted|"
    r"quotation|sum|deposit|balance|worth|value|reduced|refund|credit|surcharge|premium|tariff|comes|reduction"
)
PRICE_BEFORE = re.compile(rf"\b(?:{PRICE_WORDS})\b", re.IGNORECASE)
# right after a pricing word, a year-like number is a price too ("the total is 2000")
PRICE_JUST_BEFORE = re.compile(
    rf"\b(?:{PRICE_WORDS})\s*(?:is|was|of|at|to|will be|would be|comes to|:|=)?\s*(?:only|just|about|around)?\s*$",
    re.IGNORECASE,
)
PRICE_AFTER = re.compile(
    r"^\s*(?:off\b|in total\b|total\b|net\b|ex(?:cl\.?|cluding)?\s+(?:tax|vat)|\+\s*(?:tax|vat))", re.IGNORECASE
)
# words that may follow a price without making it a quantity ("12,500 before tax", "18,900 each")
GLUE = frozenset(
    {
        *("each", "per", "before", "after", "excluding", "excl", "incl", "including", "inclusive", "plus", "net"),
        *("gross", "for", "and", "or", "is", "was", "in", "with", "to", "instead", "rather", "than", "tax", "taxes"),
        *("vat", "ht", "ttc", "all", "apiece", "upfront", "at", "as", "which", "that", "so", "if", "but", "on", "by"),
        "from",
    }
)
MONTHS = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|"
    r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b\.?",
    re.IGNORECASE,
)
IDENT_BEFORE = re.compile(
    r"\b(?:no|nr|number|line|ref|reference|order|item|version|v|step|page|room|floor|suite|chapter|section|ticket|"
    r"case|id|code|po|model|batch|lot|week|day|days|quarter|q|year|years|fy|iso|phase|cell|station|gate|hall|booth|"
    r"zone|top|since|until|till|in|by|of the year|class|level|grade|tier|size|ext|extension)\.?\s*$",
    re.IGNORECASE,
)
PHONE_WORDS = re.compile(r"\b(?:phone|tel|telephone|call|mobile|cell|fax|whatsapp|sms|dial|reach me|ring)\b", re.I)
PHONE = re.compile(
    r"(?<![\w.,])(?:\+\d[\d ().-]{6,}\d|\(\d{2,5}\)\s?[\d .-]{4,}\d|\d{2,5}(?:[ .-]\d{2,5}){2,4})(?![\w])"
)
DATE_TIME = re.compile(
    r"\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b|\b\d{1,2}[:h]\d{2}\b|"
    r"\b\d{1,2}\s?(?:am|pm|a\.m\.|p\.m\.)(?!\w)",
    re.IGNORECASE,
)


def _is_phone(raw: str, before: str) -> bool:
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 7:
        return False
    if raw.lstrip().startswith(("+", "(")) or PHONE_WORDS.search(before[-30:]):
        return True
    groups = re.split(r"[ .-]", raw.strip())
    # 12 500 000 is a thousands grouping; 020 7946 0958 or 555-123-4567 are phone numbers
    thousands = len(groups[0]) <= 3 and all(len(g) == 3 for g in groups[1:]) and "-" not in raw
    return not thousands


def bare_amounts_in(text: str) -> list[tuple[str, float]]:
    """Numbers written without a currency that look like prices: (as written, value)."""
    text = text or ""
    taken: list[tuple[int, int]] = [m.span() for m in AMOUNT.finditer(text)]
    taken += [m.span() for m in DATE_TIME.finditer(text)]
    taken += [m.span() for m in PHONE.finditer(text) if _is_phone(m.group(0), text[: m.start()])]
    out: list[tuple[str, float]] = []
    for m in BARE.finditer(text):
        start, end = m.span("num")
        if any(s < end and start < e for s, e in taken):
            continue
        raw, after = m.group("num"), text[m.end() :]
        before = text[max(0, start - 60) : start]
        if re.match(r"\s*(?:%|percent\b|per cent\b|pc\b|pct\b)", after, re.IGNORECASE):
            continue  # a percentage
        word = re.match(r"\s*([^\W\d_]+)", after)
        if (word and MONTHS.fullmatch(word.group(1))) or MONTHS.search(before[-12:]):
            continue  # part of a date ("7 December", "December 7, 2026")
        if IDENT_BEFORE.search(before) and not PRICE_JUST_BEFORE.search(before):
            continue  # an identifier, a period or a multiplier ("line 2", "Q4", "x 6", "in 2026")
        price_after = bool(PRICE_AFTER.match(after))
        if word and word.group(1).lower() not in GLUE and not price_after:
            continue  # a quantity ("6 arms", "1,200 parts per hour", "30 days")
        value = parse_number(raw)
        if value is None:
            continue
        written_like_price = bool(m.group("sep")) or bool(re.search(r"[.,]\d{2}$", raw))
        context = bool(PRICE_BEFORE.search(" ".join(before.split()[-4:]))) or price_after
        year_like = not m.group("sep") and re.fullmatch(r"(?:19|20)\d{2}", raw) is not None
        if year_like:
            if not PRICE_JUST_BEFORE.search(before):
                continue
        elif not (written_like_price or context) or (not written_like_price and m.group("scale") and not context):
            continue
        if not written_like_price and not context and value < 10:
            continue
        out.append((m.group(0).strip(), _scaled(value, m.group("scale"))))
    return out


def ungrounded_amounts(text: str, pricing_texts: list[str]) -> list[str]:
    """Amounts in ``text`` — with a currency or bare numbers that look like prices — that appear in none of
    the pricing sources (quotations, products)."""
    known = {round(v, 2) for t in pricing_texts for m in ANY_NUMBER.finditer(t) if (v := parse_number(m.group(0)))}
    found = amounts_in(text) + bare_amounts_in(text)
    return [raw for raw, value in found if not any(abs(value - k) < 0.01 for k in known)]


# ---------------------------------------------------------------------------------------------- sources


def quotation_sources(env: Environment, lead: Model, limit: int = 3) -> list[Source]:
    quotes = env["sale.quotation"]
    if not quotes.check_access_rights("read", raise_exception=False):
        return []
    out = []
    for q in quotes.search([("opportunity_id", "=", lead.ids[0])], order="id desc", limit=limit):
        cur = q.currency_id.name if q.currency_id else None
        lines = []
        for line in q.line_ids:
            if line.display_type:
                continue
            what = line.product_id.display_name if line.product_id else (line.name or "").split("\n")[0]
            qty = f"{line.quantity:g}"
            text = f"- {qty} x {what} at {money(line.price_unit, cur)} each"
            if line.discount:
                text += f", discount {line.discount:g}%"
            text += f" = {money(line.price_subtotal, cur)}" + (" (optional)" if line.is_optional else "")
            lines.append(text)
        body = [
            f"Quotation {q.name} — state: {q.state}",
            *lines,
            f"Untaxed total: {money(q.amount_untaxed, cur)}",
            f"Taxes: {money(q.amount_tax, cur)}",
            f"Total: {money(q.amount_total, cur)}",
        ]
        if q.validity_date:
            body.append(f"Valid until: {q.validity_date.isoformat()}")
        out.append(Source(f"sale.quotation:{q.ids[0]}", f"Quotation {q.name}", "\n".join(body)))
    return out


def product_sources(env: Environment, context_text: str, exclude: set[int], limit: int = 5) -> list[Source]:
    """Catalogue products the conversation mentions by name or reference (with their list price)."""
    products = env["product.product"]
    if not products.check_access_rights("read", raise_exception=False):
        return []
    haystack = context_text.lower()
    out = []
    for p in products.search([("sale_ok", "=", True)], limit=500):
        if p.ids[0] in exclude:
            continue
        names = [n for n in (p.name, p.default_code) if n and len(n) >= 4]
        if not any(n.lower() in haystack for n in names):
            continue
        cur = p.currency_id.name if p.currency_id else None
        unit = p.uom_id.name if p.uom_id else "unit"
        text = f"{p.display_name}\nList price: {money(p.list_price, cur)} per {unit}"
        if p.description_sale:
            text += f"\nDescription: {p.description_sale}"
        out.append(Source(f"product.product:{p.ids[0]}", p.display_name, text))
        if len(out) >= limit:
            break
    return out


def pricing_sources(env: Environment, lead: Model, context_text: str) -> list[Source]:
    quotes = quotation_sources(env, lead)
    quoted = {
        line.product_id.ids[0]
        for q in env["sale.quotation"].browse([int(s.id.split(":")[1]) for s in quotes])
        for line in q.line_ids
        if line.product_id
    }
    return quotes + product_sources(env, context_text, quoted)


@handler(FEATURE)
def prepare(env: Environment, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    lead = readable_lead(env, record)
    main = lead_source(env, lead, thread=12)
    pricing = pricing_sources(env, lead, main.text)
    recipient = lead.partner_id.display_name if lead.partner_id else (lead.contact_name or lead.partner_name or "")

    def on_output(ctx: ToolContext, text: str, data: Any) -> None:
        unverified = ungrounded_amounts(text, [s.text for s in pricing])
        payload: dict[str, Any] = {"body": text}
        title = f"Follow-up e-mail to {recipient}" if recipient else "Follow-up e-mail"
        if unverified:
            payload["unverified_amounts"] = unverified
            title += " — check the amounts"
        ctx.proposals.append(PendingProposal("message", LEAD, lead.ids[0], payload, title))

    return {"sources": [main, *pricing], "on_output": on_output}


# ---------------------------------------------------------------------------------------------- evaluation


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> RunResult:
    """Default run plus the price-grounding check, exposed to the scorer through ``data``."""
    result = run_feature(env, FEATURE, inputs, record=record, **HANDLERS[FEATURE](env, record, inputs))
    unverified = [a for p in result.proposals for a in (p.payload or {}).get("unverified_amounts", [])]
    found = amounts_in(result.text) + bare_amounts_in(result.text)
    result.data = {"amounts": [raw for raw, _ in found], "unverified_amounts": unverified}
    return result


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``price_grounding``: share of drafts whose every amount appears in a quotation or product source;
    ``price_check_accuracy``: share of probe drafts (cases with ``expected.unverified_amounts``) on which the
    check reports exactly the expected amounts — fabricated bare prices caught, nothing else flagged."""
    measured = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not measured:
        return {}

    def probe(r: dict[str, Any]) -> bool:
        return "unverified_amounts" in ((r.get("_case") or {}).get("expected") or {})

    drafts = [r for r in measured if not probe(r)]
    probes = [r for r in measured if probe(r)]
    out = {"amounts_checked": float(sum(len(r["data"].get("amounts", [])) for r in measured))}
    if drafts:
        out["price_grounding"] = sum(1 for r in drafts if not r["data"].get("unverified_amounts")) / len(drafts)
    if probes:
        exact = sum(
            1
            for r in probes
            if sorted(r["data"].get("unverified_amounts") or []) == sorted(r["_case"]["expected"]["unverified_amounts"])
        )
        out["price_check_accuracy"] = exact / len(probes)
    return out
