"""``sale.quote_from_text`` — draft quotation lines from a pasted e-mail, RFP or list.

The model only **extracts** what the customer asks for (description as written, quantity, unit, product code
when one is quoted, constraints such as delivery dates or colours). Each item is then mapped to the catalogue
by deterministic matching, as the requesting user, among the products they can read and sell on the
quotation:

1. a product code quoted in the item (exact, case-insensitive),
2. name similarity — trigram coverage of the item by the product name and of the name by the item, on the
   product's name and its variant name,
3. semantic similarity — embeddings of the item and of the best candidates through the AI gateway.

An item is matched when the best candidate is good enough and clearly ahead of the next one; otherwise it is
**flagged** (no match, or ambiguous with the candidates listed) — a product is never invented and the model
never chooses a product id. Quantities must appear in the text (digits or number words); prices always come
from the pricelist when the lines are created. The matched lines are one write proposal on the quotation.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import TYPE_CHECKING, Any

from tessera.ai import Source
from tessera.ai.runtime import handler
from tessera.ai.tools import PendingProposal, ToolContext
from tessera.exceptions import AccessError, UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "sale.quote_from_text"
ACCEPT = 0.5  # combined score needed to match
CLEAR_WIN = 0.9  # above this a match does not need a margin over the runner-up
MARGIN = 0.08
W_NAME, W_EMBED = 0.7, 0.3
EMBED_TOP = 8
MAX_ITEMS = 60
MAX_TEXT = 50000
CODE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_./]{2,}")
NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:[ ,.'\u2019]\d{3})+|\d+)(?:[.,](\d+))?(?![\w])")
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "hundred": 100, "dozen": 12, "pair": 2, "couple": 2, "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4,
    "cinq": 5, "dix": 10, "douze": 12, "ein": 1, "eine": 1, "zwei": 2, "drei": 3, "vier": 4,
    "fünf": 5, "zehn": 10,
}  # fmt: skip
UNIT_ALIASES = {
    "dozen": "product.uom_dozen",
    "dozens": "product.uom_dozen",
    "day": "product.uom_day",
    "days": "product.uom_day",
    "jour": "product.uom_day",
    "jours": "product.uom_day",
    "hour": "product.uom_hour",
    "hours": "product.uom_hour",
    "h": "product.uom_hour",
    "heures": "product.uom_hour",
    "kg": "product.uom_kg",
    "g": "product.uom_gram",
    "m": "product.uom_meter",
    "cm": "product.uom_cm",
    "l": "product.uom_liter",
}
PLAIN_UNITS = {"", "unit", "units", "unit(s)", "pc", "pcs", "piece", "pieces", "each", "ea", "x", "pce", "pièces"}


def fold(text: str) -> str:
    """Lower case, accents removed, punctuation as spaces."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") and not word.endswith("ss") else word


def trigrams(text: str) -> set[str]:
    """pg_trgm-style trigrams of each word (padded with two spaces before and one after)."""
    out: set[str] = set()
    for word in fold(text).split():
        w = f"  {_singular(word)} "
        out.update(w[i : i + 3] for i in range(len(w) - 2))
    return out


def name_score(item: str, name: str) -> float:
    """How well ``item`` names a product called ``name`` (0..1): the mean of the share of the item's trigrams
    found in the name and the share of the name's trigrams found in the item (a short "robot arm" still names
    the "Collaborative robot arm"; a long name is not matched by one shared word)."""
    a, b = trigrams(item), trigrams(name)
    if not a or not b:
        return 0.0
    common = len(a & b)
    return 0.5 * common / len(a) + 0.5 * common / len(b)


def cosine(u: list[float], v: list[float]) -> float:
    dot = sum(x * y for x, y in zip(u, v, strict=False))
    nu = math.sqrt(sum(x * x for x in u)) or 1.0
    nv = math.sqrt(sum(y * y for y in v)) or 1.0
    return dot / (nu * nv)


def numbers_in(text: str) -> set[float]:
    """Every quantity the text states (digits with thousands separators or decimals, number words)."""
    found: set[float] = set()
    text = re.sub(r"(?<=\d)[xX\u00d7]|[xX\u00d7](?=\d)", " ", text or "")  # "4x", "x4", "4\u00d7"
    for m in NUMBER.finditer(text):
        whole = re.sub(r"[ ,.'\u2019]", "", m.group(1))
        try:
            found.add(float(f"{whole}.{m.group(2)}" if m.group(2) else whole))
        except ValueError:  # pragma: no cover - the pattern only matches digits
            continue
    words = fold(text).split()
    for i, w in enumerate(words):
        if w in NUMBER_WORDS:
            value = float(NUMBER_WORDS[w])
            found.add(value)
            if w == "hundred" and i and words[i - 1] in NUMBER_WORDS:
                found.add(float(NUMBER_WORDS[words[i - 1]]) * 100)
    return found


# ----------------------------------------------------------------------------- catalogue matching


def catalogue(env: Environment, order: Model) -> list[dict[str, Any]]:
    """The products the user can read and sell on ``order`` (its company or shared)."""
    company = order.elevated().company_id.ids[0]
    products = env["product.product"].search(
        [("is_sellable", "=", True), {"any": [("company_id", "is_not_set"), ("company_id", "=", company)]}],
        limit=5000,
    )
    out = []
    for p in products:
        out.append(
            {
                "id": p.ids[0],
                "name": p.name or "",
                "display_name": p.display_name or "",
                "code": (p.default_code or "").strip().upper(),
                "text": f"{p.display_name} {p.description_sale or ''}",
                "uom_id": p.uom_id.ids[0] if p.uom_id else None,
            }
        )
    return out


def _embeddings(env: Environment, texts: list[str]) -> list[list[float]] | None:
    from tessera.ai.client import CallContext, get_client
    from tessera.ai.errors import AIError

    try:
        resp = get_client(env).embed(CallContext(env.company.ids[0] if env.company else 0, env.uid, FEATURE), texts)
    except AIError:
        return None  # name and code matching still work without the gateway
    vectors: list[list[float]] = resp["vectors"]
    return vectors


def match_item(env: Environment, products: list[dict[str, Any]], item: dict[str, Any]) -> dict[str, Any]:
    """The catalogue product an extracted item asks for, or a flag (``no_match`` / ``ambiguous``)."""
    description = str(item.get("description") or "")[:500]
    codes = {str(item.get("code") or "").strip().upper()} | {t.upper() for t in CODE_TOKEN.findall(description)}
    codes.discard("")
    by_code = [p for p in products if p["code"] and p["code"] in codes]
    if len(by_code) == 1:
        return {"status": "matched", "product": by_code[0], "method": "code", "score": 1.0, "candidates": []}
    pool = by_code or products
    scored = []
    for p in pool:
        s = max(name_score(description, p["name"]), name_score(description, p["display_name"]))
        scored.append((s, p))
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    top = scored[:EMBED_TOP]
    method = "name"
    if top and description:
        vectors = _embeddings(env, [description, *[p["text"] for _, p in top]])
        if vectors:
            method = "name+semantic"
            top = [
                (W_NAME * s + W_EMBED * max(0.0, cosine(vectors[0], vectors[i + 1])), p) for i, (s, p) in enumerate(top)
            ]
            top.sort(key=lambda x: (-x[0], x[1]["id"]))
    candidates = [{"product_id": p["id"], "display_name": p["display_name"], "score": round(s, 3)} for s, p in top[:3]]
    if not top or top[0][0] < ACCEPT:
        return {"status": "no_match", "candidates": candidates}
    best, second = top[0][0], (top[1][0] if len(top) > 1 else 0.0)
    if best < CLEAR_WIN and best - second < MARGIN:
        return {"status": "ambiguous", "candidates": candidates}
    return {"status": "matched", "product": top[0][1], "method": method, "score": round(best, 3), "candidates": []}


def _uom_for(env: Environment, unit: str, product_uom_id: int | None) -> tuple[int | None, str | None]:
    """(uom id for the line, note) — a unit of another category than the product's is ignored."""
    key = fold(unit)
    if key in PLAIN_UNITS or not product_uom_id:
        return None, None
    xmlid = UNIT_ALIASES.get(key) or UNIT_ALIASES.get(_singular(key))
    uom = env.ref(xmlid, raise_if_not_found=False) if xmlid else None
    if uom is None:
        return None, _("Unit '{unit}' not understood: the product's unit is used", unit=unit)
    product_uom = env["product.uom"].elevated().browse([product_uom_id])
    if uom.elevated().category != product_uom.category:
        return None, _("Unit '{unit}' does not fit this product: the product's unit is used", unit=unit)
    return (uom.ids[0] if uom.ids[0] != product_uom_id else None), None


def map_items(env: Environment, order: Model, items: list[dict[str, Any]], text: str) -> dict[str, Any]:
    """Deterministic mapping of extracted items to draft lines and flagged items."""
    products = catalogue(env, order)
    stated = numbers_in(text)
    lines: list[dict[str, Any]] = []
    flagged: list[dict[str, Any]] = []
    for index, raw in enumerate(items[:MAX_ITEMS]):
        if not isinstance(raw, dict):
            continue
        item = {k: raw.get(k) for k in ("description", "quantity", "unit", "code", "constraints")}
        description = str(item.get("description") or "").strip()
        if not description:
            continue
        found = match_item(env, products, item)
        if found["status"] != "matched":
            flagged.append(
                {
                    "index": index,
                    "description": description,
                    "reason": found["status"],
                    "candidates": found["candidates"],
                }
            )
            continue
        product = found["product"]
        notes: list[str] = []
        qty = item.get("quantity")
        quantity = float(qty) if isinstance(qty, int | float) and not isinstance(qty, bool) else 1.0
        if not (0 < quantity <= 1e6):
            quantity = 1.0
            notes.append(_("Quantity not understood: 1 is used"))
        elif quantity != 1.0 and quantity not in stated:
            notes.append(_("Quantity {qty} is not in the text: 1 is used", qty=f"{quantity:g}"))
            quantity = 1.0
        uom_id, note = _uom_for(env, str(item.get("unit") or ""), product["uom_id"])
        if note:
            notes.append(note)
        constraints = str(item.get("constraints") or "").strip()[:300]
        lines.append(
            {
                "index": index,
                "description": description,
                "product_id": product["id"],
                "display_name": product["display_name"],
                "quantity": quantity,
                "uom_id": uom_id,
                "constraints": constraints,
                "method": found["method"],
                "score": found["score"],
                "notes": notes,
            }
        )
    return {"lines": lines, "flagged": flagged}


# ----------------------------------------------------------------------------- feature


def readable_quotation(env: Environment, record: Model | None) -> Model:
    if record is None or record._name != "sale.quotation":
        raise UserError(_("Lines from text are added to a quotation"))
    record.check_access("read")
    return record


@handler(FEATURE)
def prepare(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    order = readable_quotation(env, record)
    text = str(inputs.get("text") or "")[:MAX_TEXT]
    if not text.strip():
        raise UserError(_("Paste the customer's request first"))
    rec = order.elevated()
    source = Source(f"sale.quotation:{rec.ids[0]}", _("Request for {order}", order=rec.name), text)

    def on_output(ctx: ToolContext, _text: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        raw = data.get("items")
        items: list[dict[str, Any]] = raw if isinstance(raw, list) else []
        mapped = map_items(env, order, items, text)
        data.update(mapped, extracted=len(items))
        if not mapped["lines"] or rec.state not in ("draft", "sent") or rec.locked:
            return
        try:
            order.check_access("write")
        except AccessError:
            return
        commands = []
        for line in mapped["lines"]:
            vals: dict[str, Any] = {"product_id": line["product_id"], "quantity": line["quantity"]}
            if line["uom_id"]:
                vals["uom_id"] = line["uom_id"]
            if line["constraints"]:
                vals["name"] = f"{line['display_name']}\n{line['constraints']}"
            commands.append(["create", vals])
        title = _("Add {count} lines from the request to {order}", count=len(commands), order=rec.name)
        ctx.proposals.append(PendingProposal("write", "sale.quotation", rec.ids[0], {"line_ids": commands}, title))

    return {"sources": [source], "on_output": on_output}
