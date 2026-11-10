"""``crm.followup_draft`` — follow-up and proposal e-mails, streamed into the lead's chatter composer.

Sources (read as the requesting user): the lead with its latest discussion, its quotations line by line,
and catalogue products the conversation mentions. The draft becomes a **message proposal**; nothing is
sent until the user sends it from the composer (``proposal.accept``).

Prices: an amount may only come from a quotation or a product source. Every currency amount in the
draft is checked against the numbers in those sources (never against the lead's internal revenue
estimate); amounts that do not appear there are listed on the proposal as ``unverified_amounts`` and
counted by the evaluation (``price_grounding`` must be 1.0 — zero fabricated prices).
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
NUM = r"\d{1,3}(?:[,.  ' ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?"
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
    s = re.sub(r"[  ' ]", "", raw)
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) == 2 and len(parts[1]) in (1, 2):
            s = s.replace(sep, ".")
        else:
            s = s.replace(sep, "")
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


def ungrounded_amounts(text: str, pricing_texts: list[str]) -> list[str]:
    """Amounts in ``text`` that appear in none of the pricing sources (quotations, products)."""
    known = {round(v, 2) for t in pricing_texts for m in ANY_NUMBER.finditer(t) if (v := parse_number(m.group(0)))}
    return [raw for raw, value in amounts_in(text) if not any(abs(value - k) < 0.01 for k in known)]


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
            text = f"- {qty} × {what} at {money(line.price_unit, cur)} each"
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
    result.data = {"amounts": [raw for raw, _ in amounts_in(result.text)], "unverified_amounts": unverified}
    return result


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``price_grounding``: share of drafts whose every amount appears in a quotation or product source."""
    measured = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not measured:
        return {}
    clean = sum(1 for r in measured if not r["data"].get("unverified_amounts"))
    amounts = sum(len(r["data"].get("amounts", [])) for r in measured)
    return {"price_grounding": clean / len(measured), "amounts_checked": float(amounts)}
