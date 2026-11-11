# ruff: noqa: RUF001, RUF002 - the parser reads typographic characters (minus sign, dashes, no-break spaces)
"""Reading invoices and bills (``account.bill_ocr``): the deterministic part.

* :func:`text_layer` pulls the text out of a digital PDF (``pypdf``); scans and image-only PDFs have none
  and go to the vision model instead (``modules.account.ai_bill``).
* :func:`parse_text` reads the header (partner, VAT ID, IBAN, number, dates, currency, totals, tax lines)
  and the line table of English, French, German and Spanish documents with label vocabularies, number and
  date parsers for each convention and a column solver that only accepts a line when quantity × unit price
  equals its amount.
* :func:`from_model` normalises a model's structured answer to the same :class:`Extraction`, and
  :func:`grounding` lists the numbers of that answer that do not occur in the document's text.

Nothing here touches the database.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

MAX_PAGES = 20


# ----------------------------------------------------------------------------- text helpers


def _fold_char(ch: str) -> str:
    base = unicodedata.normalize("NFKD", ch)[:1] or ch
    low = base.lower()
    return low if len(low) == 1 else base


def fold(text: str) -> str:
    """Lower-case, accents removed, one character per input character (so spans map back)."""
    return "".join(_fold_char(c) for c in text or "")


def words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", fold(text))


def text_layer(data: bytes) -> str:
    """The text of a PDF's text layer ('' for images, image-only PDFs and unreadable files)."""
    if not data.startswith(b"%PDF"):
        return ""
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [p.extract_text() or "" for p in reader.pages[:MAX_PAGES]]
    except Exception:  # a damaged PDF is read by the model instead
        return ""
    text = "\n".join(pages)
    for space in (" ", " ", " "):
        text = text.replace(space, " ")
    # kerning pairs ("To", "Va", "We", "Ye", "P.") come out of some PDF writers as two words
    text = KERNING_SPLIT.sub(r"\1", text)
    return text if len(re.findall(r"[A-Za-z]", text)) >= 30 else ""


KERNING_SPLIT = re.compile(r"(?<![A-Za-z])([TVWYFP]) (?=[a-zà-ÿ.]{2,})")

# ----------------------------------------------------------------------------- numbers and dates

NUM = r"[-−]?\d{1,3}(?:[ '.,]\d{3})*(?:[.,]\d{1,4})?|[-−]?\d+(?:[.,]\d{1,4})?"
CURRENCY_TOKENS = ("€", "EUR", "USD", "GBP", "CHF", "£", "$", "US$")


def parse_number(token: str) -> float | None:
    """``1 234,56`` / ``1.234,56`` / ``1,234.56`` / ``1'234.56`` / ``12,5`` → float."""
    t = re.sub(r"[^\d.,\-−]", "", token.replace("−", "-"))
    if not re.search(r"\d", t):
        return None
    neg = t.startswith("-")
    t = t.replace("-", "")
    if "." in t and "," in t:
        dec = "." if t.rfind(".") > t.rfind(",") else ","
        thousands = "," if dec == "." else "."
        t = t.replace(thousands, "").replace(dec, ".")
    elif "." in t or "," in t:
        sep = "." if "." in t else ","
        head, _, tail = t.rpartition(sep)
        thousands_only = t.count(sep) > 1 or len(tail) == 3  # 1.234.567 / 1,234
        t = t.replace(sep, "") if thousands_only else f"{head.replace(sep, '') or '0'}.{tail}"
    try:
        value = float(t)
    except ValueError:
        return None
    return -value if neg else value


MONTHS = {
    # English
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
    "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    # French
    "janvier": 1, "janv": 1, "fevrier": 2, "fevr": 2, "mars": 3, "avril": 4, "avr": 4, "mai": 5,
    "juin": 6, "juillet": 7, "juil": 7, "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    # German
    "januar": 1, "februar": 2, "marz": 3, "maerz": 3, "juni": 6, "juli": 7, "oktober": 10, "okt": 10,
    "dezember": 12, "dez": 12,
    # Spanish
    "enero": 1, "ene": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "setiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12, "dic": 12,
}  # fmt: skip
MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))
DATE_PATTERNS = [
    re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b"),
    re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b"),
    re.compile(rf"\b(\d{{1,2}})\.?\s*(?:de\s+)?({MONTH_RE})\.?,?\s+(?:de\s+)?(\d{{4}})\b"),
    re.compile(rf"\b({MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b"),
]


def _mkdate(y: int, m: int, d: int) -> date | None:
    if y < 100:
        y += 2000
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(text: str) -> tuple[date | None, int]:
    """Earliest date in folded ``text`` and where it starts (-1 when none). Numeric dates are day first."""
    best: tuple[int, date] | None = None
    for i, pat in enumerate(DATE_PATTERNS):
        for m in pat.finditer(text):
            g = m.groups()
            if i == 0:
                d = _mkdate(int(g[0]), int(g[1]), int(g[2]))
            elif i == 1:
                d = _mkdate(int(g[2]), int(g[1]), int(g[0]))
            elif i == 2:
                d = _mkdate(int(g[2]), MONTHS[g[1]], int(g[0]))
            else:
                d = _mkdate(int(g[2]), MONTHS[g[0]], int(g[1]))
            if d and (best is None or m.start() < best[0]):
                best = (m.start(), d)
            break
    return (best[1], best[0]) if best else (None, -1)


# ----------------------------------------------------------------------------- vocabularies


def _label(*phrases: str) -> str:
    """Folded label phrases as a tolerant regex (``t otal`` from kerning, ``n°`` / ``no`` / ``nº``)."""
    out = []
    for p in phrases:
        parts = [r"\s?".join(re.escape(c) for c in w) for w in p.split()]
        out.append(r"[\s.:°º#'’\-]*".join(parts))
    return "(?:" + "|".join(sorted(out, key=len, reverse=True)) + ")"


def _rx(*phrases: str) -> re.Pattern[str]:
    return re.compile(_label(*phrases))


INVOICE_NO_LABELS = [
    _rx("invoice number", "invoice no", "invoice nr", "invoice #", "bill number", "bill no", "credit note number",
        "credit note no", "numero de facture", "n de facture", "no de facture", "facture n", "facture no",
        "n d avoir", "avoir n", "rechnungsnummer", "rechnungs nr", "rechnung nr", "gutschrift nr", "gutschriftsnummer",
        "numero de factura", "n de factura", "factura n", "factura no", "num factura", "nota de credito n"),
    _rx("invoice", "facture", "rechnung", "factura", "avoir", "gutschrift"),
]  # fmt: skip
DUE_LABELS = [
    _rx("due date", "payment due", "due by", "pay by", "date d echeance", "echeance", "a payer avant le",
        "a regler avant le", "faellig am", "fallig am", "falligkeitsdatum", "faelligkeitsdatum", "zahlbar bis",
        "fecha de vencimiento", "vencimiento", "fecha vencimiento"),
]  # fmt: skip
DATE_LABELS = [
    _rx("invoice date", "date of issue", "issue date", "date issued", "date de facture", "date d emission",
        "date de facturation", "rechnungsdatum", "ausstellungsdatum", "fecha de factura", "fecha de emision",
        "fecha factura"),
    _rx("date", "datum", "fecha", "dated", "le"),
]  # fmt: skip
VAT_ID_LABEL = _rx(
    "vat number",
    "vat no",
    "vat reg no",
    "vat registration",
    "vat id",
    "tax id",
    "n tva intracommunautaire",
    "tva intracommunautaire",
    "tva intracom",
    "n tva",
    "no tva",
    "ust idnr",
    "ust id nr",
    "ust id",
    "ustid",
    "umsatzsteuer id",
    "steuernummer",
    "nif",
    "cif",
    "n i f",
)
BUYER_HEADINGS = _rx(
    "bill to",
    "billed to",
    "invoice to",
    "sold to",
    "customer",
    "client",
    "facture a",
    "adresse de facturation",
    "destinataire",
    "rechnungsempfanger",
    "rechnung an",
    "kunde",
    "cliente",
    "facturar a",
    "datos del cliente",
)
SELLER_HEADINGS = _rx(
    "from", "seller", "supplier", "vendeur", "fournisseur", "lieferant", "verkaufer", "proveedor", "emisor"
)
SUBTOTAL = _rx(
    "subtotal",
    "sub total",
    "net total",
    "total net",
    "total excl vat",
    "total excl tax",
    "total excluding vat",
    "total before tax",
    "net amount",
    "amount excl vat",
    "total ht",
    "montant ht",
    "total hors taxes",
    "sous total",
    "nettobetrag",
    "summe netto",
    "gesamt netto",
    "zwischensumme",
    "netto",
    "base imponible",
    "importe neto",
    "total sin iva",
    "total neto",
)
TAXLINE = _rx("vat", "tva", "mwst", "ust", "umsatzsteuer", "mehrwertsteuer", "iva", "sales tax", "tax")
TOTAL = _rx(
    "total due",
    "amount due",
    "balance due",
    "grand total",
    "total amount",
    "invoice total",
    "total ttc",
    "net a payer",
    "montant ttc",
    "total a payer",
    "gesamtbetrag",
    "rechnungsbetrag",
    "endbetrag",
    "zu zahlen",
    "gesamtsumme",
    "total factura",
    "importe total",
    "total a pagar",
    "total",
)
REVERSE_CHARGE = _rx(
    "reverse charge",
    "autoliquidation",
    "auto liquidation",
    "steuerschuldnerschaft des leistungsempfangers",
    "reverse charge verfahren",
    "inversion del sujeto pasivo",
    "sujeto pasivo",
    "article 196",
    "artikel 196",
    "art 196",
    "article 283",
)
CREDIT_NOTE = _rx("credit note", "avoir", "gutschrift", "nota de credito", "factura rectificativa", "abono")
TITLE_WORDS = {
    "invoice", "tax invoice", "bill", "facture", "rechnung", "factura", "credit note", "avoir", "gutschrift",
    "nota de credito", "page", "seite", "pagina",
}  # fmt: skip
LEGAL_FORMS = (
    r"(?:S\.?A\.?R\.?L\.?|S\.?A\.?S\.?U?|SASU|SELARL|SCP|SNC|EURL|SCOP|GmbH(?:\s*&\s*Co\.?\s*KG)?|AG|KG|OHG|UG|e\.K\.|"
    r"S\.L\.U?\.?|SLU|SL|S\.A\.U?\.?|SA|S\.C\.|Ltd\.?|Limited|LLC|LLP|Inc\.?|Corp\.?|PLC|plc|B\.V\.|BV|N\.V\.|"
    r"S\.r\.l\.|SRL|S\.p\.A\.|Oy|AB|ApS|A/S)"
)
LEGAL_RE = re.compile(rf"^(.{{2,80}}?\s{LEGAL_FORMS})(?=$|[\s,;|–—·(-])")
COLUMN_WORDS = {
    "description": ["description", "designation", "libelle", "article", "item", "service", "produit", "product",
                    "bezeichnung", "beschreibung", "artikel", "leistung", "concepto", "descripcion", "detalle",
                    "articulo"],
    "quantity": ["qty", "quantity", "qte", "quantite", "menge", "anzahl", "anz", "cantidad", "cant", "uds"],
    "unit_price": ["unit price", "price", "prix unitaire", "pu ht", "p u ht", "prix", "einzelpreis", "e preis",
                   "preis", "precio unitario", "precio", "rate"],
    "tax_rate": ["vat", "tva", "mwst", "ust", "iva", "tax", "taux"],
    "amount": ["line total", "amount", "total", "montant ht", "montant", "betrag", "gesamtpreis", "gesamt",
               "importe", "total ht", "subtotal"],
}  # fmt: skip
CURRENCIES = {"EUR": ("€", "EUR"), "USD": ("US$", "USD", "$"), "GBP": ("£", "GBP"), "CHF": ("CHF",)}
IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?)\b")
VAT_RE = re.compile(
    r"\b(ATU\d{8}|BE[01]\d{9}|DE\d{9}|DK\d{8}|EL\d{9}|ES[A-Z0-9]\d{7}[A-Z0-9]|FI\d{8}|FR[A-HJ-NP-Z0-9]{2}\d{9}|"
    r"IE\d{7}[A-W][A-I]?|IT\d{11}|LU\d{8}|NL\d{9}B\d{2}|PL\d{10}|PT\d{9}|SE\d{12}|GB\d{9}(?:\d{3})?|"
    r"CHE\d{9}(?:MWST|TVA|IVA)?)\b"
)
INVOICE_NO_VALUE = re.compile(r"([A-Za-z0-9][A-Za-z0-9\-/_.]*\d[A-Za-z0-9\-/_]*)")
NO_TOKEN = re.compile(r"^[\s.:°º#]*(?:(?:no|nr|num|n|nº|n°)(?=[\s.:°º#]))?[\s.:°º#]*", re.I)


def iban_valid(value: str) -> bool:
    v = re.sub(r"\s", "", value or "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", v):
        return False
    digits = "".join(str(int(c, 36)) for c in v[4:] + v[:4])
    return int(digits) % 97 == 1


def norm_vat(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def norm_iban(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _starts(rx: re.Pattern[str], folded: str) -> re.Match[str] | None:
    """``rx`` at the start of a folded line, as a whole word."""
    m = rx.match(folded)
    if m and (m.end() == len(folded) or not folded[m.end()].isalpha()):
        return m
    return None


# ----------------------------------------------------------------------------- result


@dataclass
class Line:
    description: str
    quantity: float
    unit_price: float
    amount: float
    tax_rate: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Extraction:
    partner_name: str | None = None
    partner_vat: str | None = None
    iban: str | None = None
    invoice_number: str | None = None
    invoice_date: date | None = None
    due_date: date | None = None
    currency: str | None = None
    amount_untaxed: float | None = None
    amount_tax: float | None = None
    amount_total: float | None = None
    tax_lines: list[dict[str, float]] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    document_type: str = "invoice"  # invoice | credit_note
    reverse_charge: bool = False
    seller_is_company: bool = False
    method: str = "text"  # text | model | vision
    text: str = ""

    HEADER = (
        "partner_name",
        "partner_vat",
        "iban",
        "invoice_number",
        "invoice_date",
        "due_date",
        "currency",
        "amount_untaxed",
        "amount_tax",
        "amount_total",
    )

    def header(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k in self.HEADER:
            v = getattr(self, k)
            out[k] = v.isoformat() if isinstance(v, date) else v
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.header(),
            "tax_lines": self.tax_lines,
            "lines": [ln.to_dict() for ln in self.lines],
            "document_type": self.document_type,
            "reverse_charge": self.reverse_charge,
            "seller_is_company": self.seller_is_company,
            "method": self.method,
        }

    def missing(self) -> list[str]:
        out = [k for k in ("invoice_number", "invoice_date", "amount_total") if getattr(self, k) in (None, "")]
        if not (self.partner_name or self.partner_vat):
            out.append("partner")
        if not self.lines:
            out.append("lines")
        return out

    def consistent(self) -> bool:
        """Lines add up to the untaxed amount and untaxed + tax = total."""
        if not self.lines or self.amount_total is None:
            return False
        lines = round(sum(ln.amount for ln in self.lines), 2)
        untaxed = self.amount_untaxed if self.amount_untaxed is not None else lines
        tax = self.amount_tax or 0.0
        return abs(lines - untaxed) <= 0.02 and abs(untaxed + tax - self.amount_total) <= 0.02


# ----------------------------------------------------------------------------- line table

NUMERIC_PART = re.compile(r"(?:US\$|[£$€])?[-−]?[\d.,']*\d[\d.,']*(?:%|€)?|%|€|EUR|USD|GBP|CHF|£|\$|US\$")
CURRENCY_AFFIX = re.compile(r"^(?:US\$|[£$€])|€$")


def _tail_parts(row: str) -> tuple[str, list[str]]:
    """Split a table row into its text and its trailing numeric parts."""
    parts = row.split()
    i = len(parts)
    while i > 0 and NUMERIC_PART.fullmatch(parts[i - 1]):
        i -= 1
    return " ".join(parts[:i]), parts[i:]


def segmentations(parts: list[str]) -> list[list[tuple[float, bool]]]:
    """Every way of reading numeric parts as numbers, fewest numbers first (digit groups of three may
    continue a number written with spaces). Each number carries whether it is a percentage."""
    tokens = [CURRENCY_AFFIX.sub("", p) for p in parts if p not in CURRENCY_TOKENS]
    out: list[list[tuple[float, bool]]] = []

    def walk(i: int, acc: list[tuple[float, bool]]) -> None:
        if len(out) > 300:
            return
        if i == len(tokens):
            out.append(list(acc))
            return
        if tokens[i] == "%":
            if acc and not acc[-1][1]:
                walk(i + 1, [*acc[:-1], (acc[-1][0], True)])
            return
        for j in range(i, min(len(tokens), i + 4)):
            group = tokens[i : j + 1]
            if j > i and not (
                re.fullmatch(r"[-−]?\d{1,3}", group[0])
                and all(re.fullmatch(r"\d{3}(?:[.,]\d{1,4})?%?", g) for g in group[1:])
            ):
                break
            if any(g.endswith("%") for g in group[:-1]):
                break
            raw = " ".join(group)
            value = parse_number(raw.rstrip("%"))
            if value is not None:
                walk(j + 1, [*acc, (value, raw.endswith("%"))])

    walk(0, [])
    return sorted(out, key=len)


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= max(0.011, abs(b) * 0.001)


def solve_row(parts: list[str], columns: list[str]) -> tuple[float, float, float, float | None] | None:
    """(quantity, unit price, amount, tax rate) from a row's numeric parts, given the order of the numeric
    columns when the table header told it; only readings where quantity × unit price = amount count."""
    for seg in segmentations(parts):
        pcts = [v for v, pct in seg if pct]
        if columns:
            if len(seg) != len(columns):
                continue
            vals = {c: v for c, (v, _pct) in zip(columns, seg, strict=True)}
            q, p, a = vals.get("quantity", 1.0), vals.get("unit_price"), vals.get("amount")
            rate = vals.get("tax_rate", pcts[0] if pcts else None)
            if p is not None and a is not None and _close(q * p, a):
                return q, p, a, rate
            continue
        nums = [v for v, pct in seg if not pct]
        if len(nums) >= 3 and nums[0] > 0 and _close(nums[0] * nums[1], nums[-1]):
            return nums[0], nums[1], nums[-1], pcts[0] if pcts else None
    return None


def _header_columns(line: str) -> list[str] | None:
    f = " " + " ".join(words(line)) + " "
    found: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for col in ("unit_price", "description", "quantity", "amount", "tax_rate"):
        best = None
        for n in sorted(COLUMN_WORDS[col], key=len, reverse=True):
            for m in re.finditer(rf"(?<=\s){re.escape(n)}(?=\s)", f):
                if any(s <= m.start() < e for s, e in taken):
                    continue
                if best is None or m.start() < best[0]:
                    best = (m.start(), m.end())
                break
        if best is not None:
            found.append((best[0], col))
            taken.append(best)
    cols = {c for _, c in found}
    if "description" in cols and "amount" in cols and ({"quantity", "unit_price"} & cols):
        return [c for _, c in sorted(found)]
    return None


# ----------------------------------------------------------------------------- the parser


def amount_of(text: str) -> float | None:
    """The amount at the end of a totals line (``TVA 20 % 42,00 €`` → 42.0, ``Total 4 208,25 €`` → 4208.25)."""
    _, parts = _tail_parts(text)
    for seg in segmentations(parts):
        nums = [v for v, pct in seg if not pct]
        if nums:
            return nums[-1]
    return None


def rate_of(text: str) -> float | None:
    m = re.search(r"(\d{1,2}(?:[.,]\d{1,2})?)\s?%", text)
    return parse_number(m.group(1)) if m else None


def _rest(line: str, folded: str, rx: re.Pattern[str]) -> str | None:
    """What follows a label anywhere in the line (as a whole word), or None."""
    for m in rx.finditer(folded):
        if m.start() > 0 and folded[m.start() - 1].isalnum():
            continue
        if m.end() < len(folded) and folded[m.end()].isalpha():
            continue
        return line[m.end() :]
    return None


def _date_value(rest: str, nxt: str | None) -> date | None:
    stripped = re.sub(r"^[\s.:°º#,]*", "", rest)
    if len(stripped) < 24 and nxt is not None:
        stripped = f"{stripped} {nxt.strip()}".strip()  # a value wrapped onto the next line
    d, at = parse_date(fold(stripped[:40]))
    return d if d is not None and at <= 1 else None


def parse_text(
    text: str, *, company_names: list[str] | None = None, company_vats: list[str] | None = None
) -> Extraction:
    """Read a document's text layer (see the module docstring)."""
    ex = Extraction(method="text", text=text)
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    folded = [fold(ln) for ln in lines]
    own_names = {" ".join(words(n)) for n in company_names or [] if n}
    own_vats = {norm_vat(v) for v in company_vats or [] if v}
    whole = "\n".join(folded)

    def is_own(line: str) -> bool:
        w = " ".join(words(line))
        return bool(w) and any(n and w.startswith(n) for n in own_names)

    # the buyer block: its heading and the lines after it
    buyer: set[int] = set()
    heading = -1
    for i, f in enumerate(folded):
        m = _starts(BUYER_HEADINGS, f)
        if m:
            rest = lines[i][m.end() :].strip(" :")
            if rest:
                lines[i], folded[i] = rest, fold(rest)
                buyer.add(i)
            else:
                buyer.add(i)
            for k in range(i + 1, min(len(lines), i + 5)):
                if _starts(SELLER_HEADINGS, folded[k]) or _header_columns(lines[k]):
                    break
                if len(buyer) > 1 and LEGAL_RE.match(lines[k]) and not is_own(lines[k]):
                    break  # the next party's block
                buyer.add(k)
                if VAT_RE.search(_joined(lines[k])) or _starts(VAT_ID_LABEL, folded[k]):
                    break
            if not rest:
                heading = i
            break

    # the line table
    table: set[int] = set()
    header_at = next((i for i, ln in enumerate(lines) if _header_columns(ln)), None)
    if header_at is not None:
        ordered = _header_columns(lines[header_at]) or []
        columns = [c for c in ordered if c != "description"]
        desc_first = ordered[0] == "description"
        pos_first = bool(re.match(r"^(?:pos|position|#|nr|no|n°|line|ligne|lfd)\b", folded[header_at]))
        table.add(header_at)
        for i in range(header_at + 1, len(lines)):
            ln, f = lines[i], folded[i]
            if _starts(SUBTOTAL, f) or _starts(TOTAL, f):
                break
            lead: list[str] = []
            body = re.sub(r"^\d{1,3}\.?\s+", "", ln) if pos_first else ln
            if not desc_first:
                m = re.match(r"^\s*([\d.,]+)\s+(.*)$", body)
                if not m:
                    continue
                lead, body = [m.group(1)], m.group(2)
            desc, parts = _tail_parts(body)
            if not desc or not parts or not re.search(r"[A-Za-zÀ-ÿ]", desc):
                continue
            solved = solve_row([*lead, *parts], columns)
            if solved is None and desc_first:
                # the description may end with digits that belong to it ("Licence Q1 2026")
                tokens = desc.split()
                for k in range(len(tokens) - 1, 0, -1):
                    if not re.fullmatch(r"[\d.,]+", tokens[k]):
                        break
                    solved = solve_row([*tokens[k:], *parts], columns)
                    if solved:
                        desc = " ".join(tokens[:k])
                        break
            if solved is None:
                continue
            q, p, a, rate = solved
            ex.lines.append(Line(desc.strip(" -–:|"), q, p, a, rate))
            table.add(i)

    # totals and tax lines
    for i, ln in enumerate(lines):
        f = folded[i]
        if i in table:
            continue
        if _starts(SUBTOTAL, f):
            if ex.amount_untaxed is None:
                ex.amount_untaxed = amount_of(ln)
        elif _starts(TOTAL, f):
            amount = amount_of(ln)
            if amount is not None and (ex.amount_total is None or amount > ex.amount_total):
                ex.amount_total = amount
        elif _starts(TAXLINE, f) and re.search(r"\d", f):
            if _starts(VAT_ID_LABEL, f) or VAT_RE.search(_joined(ln)):
                continue  # "VAT No: FR40…" is an identifier
            amount = amount_of(ln)
            rate = rate_of(ln)
            if amount is not None and not (rate is not None and amount == rate and "%" in ln.split()[-1]):
                ex.tax_lines.append({"rate": rate if rate is not None else -1.0, "amount": amount})
    if ex.tax_lines:
        ex.amount_tax = round(sum(t["amount"] for t in ex.tax_lines), 2)
    if ex.amount_untaxed is None and ex.amount_total is not None and ex.amount_tax is not None:
        ex.amount_untaxed = round(ex.amount_total - ex.amount_tax, 2)
    if ex.amount_tax is None and ex.amount_total is not None and ex.amount_untaxed is not None:
        ex.amount_tax = round(ex.amount_total - ex.amount_untaxed, 2)
    ex.reverse_charge = bool(REVERSE_CHARGE.search(whole))
    rates = {t["rate"] for t in ex.tax_lines if t["rate"] >= 0}
    if ex.lines and all(ln.tax_rate is None for ln in ex.lines):
        if len(rates) == 1:
            only = next(iter(rates))
            for line in ex.lines:
                line.tax_rate = only
        elif ex.reverse_charge or (ex.amount_tax == 0 and not rates):
            for line in ex.lines:
                line.tax_rate = 0.0

    # number and dates
    nxt = [*lines[1:], ""]
    for rx in INVOICE_NO_LABELS:
        for i, ln in enumerate(lines):
            if i in table or ex.invoice_number:
                continue
            rest = _rest(ln, folded[i], rx)
            if rest is None:
                continue
            rest = NO_TOKEN.sub("", rest)
            v = INVOICE_NO_VALUE.match(rest) or (INVOICE_NO_VALUE.match(nxt[i].strip()) if not rest.strip() else None)
            if v and parse_date(fold(v.group(1)))[0] is None and not re.fullmatch(r"\d{1,2}", v.group(1)):
                ex.invoice_number = v.group(1).rstrip(".")
        if ex.invoice_number:
            break
    for key, labels in (("due_date", DUE_LABELS), ("invoice_date", DATE_LABELS)):
        for rx in labels:
            for i, ln in enumerate(lines):
                if i in table or getattr(ex, key):
                    continue
                rest = _rest(ln, folded[i], rx)
                if rest is None:
                    continue
                d = _date_value(rest, nxt[i])
                if d and not (key == "invoice_date" and d == ex.due_date and _rest(ln, folded[i], DUE_LABELS[0])):
                    setattr(ex, key, d)
            if getattr(ex, key):
                break
    if ex.invoice_date is None:
        ex.invoice_date = parse_date(whole)[0]

    # identifiers
    vats: list[tuple[int, str]] = []
    for i, ln in enumerate(lines):
        for m in VAT_RE.finditer(_joined(ln)):
            vats.append((i, m.group(1)))
    seller_vats = [v for i, v in vats if i not in buyer and v not in own_vats]
    buyer_vats = [v for i, v in vats if i in buyer and v not in own_vats]
    top = [ln for i, ln in enumerate(lines) if i not in buyer and i not in table][:6]
    ex.seller_is_company = bool(own_vats & {v for i, v in vats if i not in buyer}) or (
        any(is_own(ln) for ln in top) and not any(is_own(lines[i]) for i in buyer)
    )
    if ex.seller_is_company:
        ex.partner_vat = buyer_vats[0] if buyer_vats else None
        names = [lines[i] for i in sorted(buyer) if i != heading and re.search(r"[A-Za-z]{2}", lines[i])]
        ex.partner_name = _clean_name(names[0]) if names else None
    else:
        ex.partner_vat = seller_vats[0] if seller_vats else None
        upper = text.upper()
        ibans = [m for m in IBAN_RE.finditer(upper) if iban_valid(m.group(1))]
        labelled = [
            m for m in ibans if re.search(r"(?:IBAN|KONTO|COMPTE|CUENTA)\s*[:.]?\s*$", upper[: m.start()][-14:])
        ]
        if ibans:
            ex.iban = norm_iban((labelled or ibans)[0].group(1))
        ex.partner_name = _seller_name(lines, folded, buyer | table, is_own)
    ex.currency = _currency(text)
    if CREDIT_NOTE.search("\n".join(folded[:15])):
        ex.document_type = "credit_note"
    return ex


def _joined(line: str) -> str:
    """Upper case with the spaces and dots inside identifiers removed (``FR 40 303 265 045``)."""
    return re.sub(r"(?<=[A-Z0-9])[ .](?=[A-Z0-9])", "", line.upper())


def _clean_name(line: str) -> str:
    line = re.split(r"\s[–—|·]\s|\s-\s|,\s(?=\d)", line)[0]
    return line.strip(" :,;")


def _seller_name(lines: list[str], folded: list[str], skip: set[int], is_own: Any) -> str | None:
    for i, ln in enumerate(lines):
        m = _starts(SELLER_HEADINGS, folded[i])
        if i not in skip and m and ln[m.end() :].strip(" :"):
            return _clean_name(ln[m.end() :].strip(" :"))
    candidates = []
    for i, ln in enumerate(lines):
        if i in skip or is_own(ln):
            continue
        if re.match(r"^(?:iban|bic|account|bank|banque|bankverbindung|banco|titulaire|inhaber|holder)", folded[i]):
            continue
        m = LEGAL_RE.match(ln)
        if m:
            candidates.append(_clean_name(m.group(1)))
    if candidates:
        return min(candidates, key=len)
    for i, ln in enumerate(lines[:8]):
        if i in skip or is_own(ln) or " ".join(words(ln)) in TITLE_WORDS:
            continue
        if re.search(r"[A-Za-z]{3}", ln) and not re.search(r"\d{3}", ln):
            return _clean_name(ln)
    return None


def _currency(text: str) -> str | None:
    counts = {code: sum(text.count(sym) for sym in syms) for code, syms in CURRENCIES.items()}
    counts["USD"] -= 2 * text.count("US$")  # "US$" also counted as "$" and "USD"-less symbol
    best = max(counts, key=lambda c: counts[c])
    return best if counts[best] > 0 else None


# ----------------------------------------------------------------------------- model answers


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, int | float):
        return float(v)
    return parse_number(str(v))


def _date(v: Any) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return parse_date(fold(str(v)))[0]


def from_model(data: Any, *, method: str, text: str = "") -> Extraction:
    """A model's structured answer (``ai/prompts/bill_ocr.yaml``) as an :class:`Extraction`."""
    ex = Extraction(method=method, text=text)
    if not isinstance(data, dict):
        return ex
    for k in ("partner_name", "partner_vat", "iban", "invoice_number", "currency"):
        v = data.get(k)
        setattr(ex, k, str(v).strip() if v not in (None, "") else None)
    ex.partner_vat = norm_vat(ex.partner_vat) or None
    ex.iban = norm_iban(ex.iban) if ex.iban and iban_valid(ex.iban) else None
    ex.currency = ex.currency.upper()[:3] if ex.currency else None
    ex.invoice_date = _date(data.get("invoice_date"))
    ex.due_date = _date(data.get("due_date"))
    for k in ("amount_untaxed", "amount_tax", "amount_total"):
        setattr(ex, k, _num(data.get(k)))
    for ln in data.get("lines") or []:
        if not isinstance(ln, dict):
            continue
        q, p, a = _num(ln.get("quantity")), _num(ln.get("unit_price")), _num(ln.get("amount"))
        q = q if q is not None else 1.0
        if a is None and p is not None:
            a = round(q * p, 2)
        if p is None and a is not None:
            p = round(a / (q or 1.0), 4)
        if a is None or p is None:
            continue
        ex.lines.append(Line(str(ln.get("description") or "").strip(), q, p, a, _num(ln.get("tax_rate"))))
    for t in data.get("tax_lines") or []:
        if isinstance(t, dict):
            rate = _num(t.get("rate"))
            ex.tax_lines.append({"rate": rate if rate is not None else -1.0, "amount": _num(t.get("amount")) or 0.0})
    ex.document_type = "credit_note" if data.get("document_type") == "credit_note" else "invoice"
    ex.reverse_charge = bool(data.get("reverse_charge"))
    ex.seller_is_company = bool(data.get("issued_by_us"))
    return ex


def grounding(ex: Extraction, text: str) -> list[float]:
    """Amounts of a model's reading that do not occur in the document's text layer (empty for scans)."""
    if not text:
        return []
    present: set[float] = set()
    tokens = text.replace("\n", " \n ").split()
    for i in range(len(tokens)):
        for j in range(i, min(len(tokens), i + 3)):
            for seg in (
                segmentations(tokens[i : j + 1]) if all(NUMERIC_PART.fullmatch(t) for t in tokens[i : j + 1]) else []
            ):
                present.update(round(v, 2) for v, _ in seg)
    numbers = [ex.amount_untaxed, ex.amount_tax, ex.amount_total]
    for ln in ex.lines:
        numbers += [ln.unit_price, ln.amount]
    return [n for n in numbers if n is not None and n != 0 and round(n, 2) not in present]
