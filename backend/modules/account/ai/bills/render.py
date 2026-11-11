# ruff: noqa: E501, S311, RUF001 - localised layouts (narrow no-break spaces), seeded randomness
"""Rendering evaluation bills: HTML in four layouts and four languages → PDF (WeasyPrint), degraded scans
(Pillow) and image-only PDFs. Deterministic for a given spec (its ``seed`` drives every random choice)."""

from __future__ import annotations

import base64
import datetime as dt
import html
import io
import random
from functools import lru_cache
from typing import Any

L10N: dict[str, dict[str, str]] = {
    "en": {"title": "INVOICE", "credit": "CREDIT NOTE", "number": "Invoice No.", "credit_number": "Credit note No.",
           "date": "Invoice date", "date_short": "Date", "due": "Due date", "bill_to": "Bill to", "from": "From",
           "desc": "Description", "qty": "Qty", "price": "Unit price", "vat": "VAT", "amount": "Amount",
           "subtotal": "Subtotal", "tax": "VAT", "total": "Total due", "vat_id": "VAT No.", "iban": "IBAN",
           "rc": "Reverse charge: VAT to be accounted for by the customer (Article 196, Directive 2006/112/EC).",
           "import": "No VAT charged: supply from outside the EU.", "terms": "Payment by bank transfer.",
           "thanks": "Thank you for your business.", "pos": "Pos."},
    "fr": {"title": "FACTURE", "credit": "AVOIR", "number": "N° de facture", "credit_number": "N° d'avoir",
           "date": "Date de facture", "date_short": "Date", "due": "Date d'échéance", "bill_to": "Facturé à",
           "from": "Fournisseur", "desc": "Désignation", "qty": "Qté", "price": "P.U. HT", "vat": "TVA",
           "amount": "Montant HT", "subtotal": "Total HT", "tax": "TVA", "total": "Total TTC",
           "vat_id": "N° TVA intracommunautaire", "iban": "IBAN", "rc": "Autoliquidation - article 283-2 du CGI.",
           "import": "TVA non applicable.", "terms": "Paiement par virement bancaire.",
           "thanks": "Merci de votre confiance.", "pos": "Pos."},
    "de": {"title": "RECHNUNG", "credit": "GUTSCHRIFT", "number": "Rechnungsnummer", "credit_number": "Gutschrift Nr.",
           "date": "Rechnungsdatum", "date_short": "Datum", "due": "Fällig am", "bill_to": "Rechnungsempfänger",
           "from": "Lieferant", "desc": "Bezeichnung", "qty": "Menge", "price": "Einzelpreis", "vat": "MwSt.",
           "amount": "Betrag", "subtotal": "Nettobetrag", "tax": "MwSt.", "total": "Gesamtbetrag",
           "vat_id": "USt-IdNr.", "iban": "IBAN", "rc": "Steuerschuldnerschaft des Leistungsempfängers (Reverse Charge).",
           "import": "Keine Umsatzsteuer.", "terms": "Zahlbar per Überweisung.", "thanks": "Vielen Dank für Ihren Auftrag.",
           "pos": "Pos."},
    "es": {"title": "FACTURA", "credit": "FACTURA RECTIFICATIVA", "number": "Número de factura",
           "credit_number": "Número de factura", "date": "Fecha de factura", "date_short": "Fecha",
           "due": "Fecha de vencimiento", "bill_to": "Facturar a", "from": "Proveedor", "desc": "Concepto",
           "qty": "Cantidad", "price": "Precio unitario", "vat": "IVA", "amount": "Importe",
           "subtotal": "Base imponible", "tax": "IVA", "total": "Total factura", "vat_id": "NIF-IVA", "iban": "IBAN",
           "rc": "Inversión del sujeto pasivo (art. 196 Directiva 2006/112/CE).", "import": "Operación no sujeta a IVA.",
           "terms": "Pago por transferencia bancaria.", "thanks": "Gracias por su confianza.", "pos": "Pos."},
}  # fmt: skip
SYMBOL = {"EUR": "€", "GBP": "£", "USD": "$"}
MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
    "fr": ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"],
    "de": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"],
}  # fmt: skip


def fmt_number(value: float, lang: str, decimals: int = 2) -> str:
    raw = f"{value:,.{decimals}f}"  # 1,234.56
    if lang == "en":
        return raw
    if lang == "fr":
        return raw.replace(",", " ").replace(".", ",")
    return raw.replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_qty(value: float, lang: str) -> str:
    return str(int(value)) if value == int(value) else fmt_number(value, lang)


def fmt_money(value: float, lang: str, currency: str) -> str:
    sym = SYMBOL.get(currency, currency)
    num = fmt_number(value, lang)
    if lang == "en" and currency in ("GBP", "USD"):
        return f"{sym}{num}"
    return f"{num} {sym}"


def fmt_rate(rate: float, lang: str) -> str:
    r = fmt_number(rate, lang, 1).replace(",0", "").replace(".0", "") if rate != int(rate) else str(int(rate))
    return f"{r}%" if lang == "en" else f"{r} %"


def fmt_date(value: str | None, lang: str, style: int) -> str:
    if not value:
        return ""
    d = dt.date.fromisoformat(value)
    month = MONTHS[lang][d.month - 1]
    if style == 0:
        return {"de": d.strftime("%d.%m.%Y")}.get(lang, d.strftime("%d/%m/%Y"))
    if lang == "en":
        return f"{month} {d.day}, {d.year}" if style == 1 else f"{d.day} {month} {d.year}"
    if lang == "es":
        return f"{d.day} de {month} de {d.year}"
    if lang == "de":
        return f"{d.day}. {month} {d.year}"
    return f"{d.day} {month} {d.year}"


CSS = """
@page { size: A4; margin: 14mm 16mm }
body { font-family: 'DejaVu Sans', sans-serif; font-size: 9pt; color: #222 }
h1 { font-size: 17pt; margin: 0 0 6px 0 }
table { border-collapse: collapse; width: 100% }
th { text-align: left; border-bottom: 1px solid #555; padding: 3px 4px }
td { padding: 3px 4px; vertical-align: top }
.num { text-align: right; white-space: nowrap }
.box { margin-bottom: 10px }
.totals { width: 48%; margin-left: 52%; margin-top: 8px }
.muted { color: #555; font-size: 8pt }
.bar { background: #1f3b5a; color: white; padding: 8px; font-size: 13pt; font-weight: bold }
"""


def _esc(v: Any) -> str:
    return html.escape(str(v or ""))


def document_html(spec: dict[str, Any]) -> str:
    rnd = random.Random(spec["seed"])
    lang, cur = spec["lang"], spec["currency"]
    t = L10N[lang]
    style = rnd.randrange(3)
    seller, buyer = spec["seller"], spec["buyer"]
    title = t["credit"] if spec.get("credit_note") else t["title"]
    number_label = t["credit_number"] if spec.get("credit_note") else t["number"]
    vat_line = f"{t['vat_id']} : {seller['vat']}" if seller.get("vat") else ""
    seller_block = (
        f"<div class='box'><b>{_esc(seller['name'])}</b><br>{_esc(seller['address'])}<br>{_esc(vat_line)}</div>"
    )
    buyer_addr = (buyer.get("street") and f"{buyer['street']}, {buyer['city']}") or buyer.get("address", "")
    buyer_vat = f"<br>{_esc(t['vat_id'])} : {_esc(buyer['vat'])}" if buyer.get("vat") else ""
    buyer_block = (
        f"<div class='box'><b>{_esc(t['bill_to'])}</b><br>{_esc(buyer['name'])}<br>{_esc(buyer_addr)}{buyer_vat}</div>"
    )
    dates = [(number_label, spec["invoice_number"]), (t["date"], fmt_date(spec["invoice_date"], lang, style))]
    if spec.get("due_date"):
        dates.append((t["due"], fmt_date(spec["due_date"], lang, style)))
    layout = spec["layout"]
    show_vat_col = layout in ("classic", "modern")
    rows = []
    for i, ln in enumerate(spec["lines"], start=1):
        cells = {
            "pos": str(i),
            "desc": _esc(ln["description"]),
            "qty": fmt_qty(ln["quantity"], lang),
            "price": fmt_number(ln["unit_price"], lang),
            "vat": fmt_rate(ln["tax_rate"], lang),
            "amount": fmt_number(ln["amount"], lang),
        }
        rows.append(cells)
    if layout == "letter":
        order = ["qty", "desc", "price", "amount"]
    elif layout == "compact":
        order = ["pos", "desc", "qty", "price", "amount"]
    elif layout == "modern":
        order = ["desc", "qty", "price", "amount", "vat"]
    else:
        order = ["desc", "qty", "price", "vat", "amount"]
    if not show_vat_col and "vat" in order:
        order.remove("vat")
    head = "".join(f"<th class='{'num' if k != 'desc' else ''}'>{_esc(t[k])}</th>" for k in order)
    body = "".join(
        "<tr>" + "".join(f"<td class='{'num' if k != 'desc' else ''}'>{r[k]}</td>" for k in order) + "</tr>"
        for r in rows
    )
    table = f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    tot = [(t["subtotal"], fmt_money(spec["amount_untaxed"], lang, cur))]
    for tl in spec["tax_lines"]:
        tot.append((f"{t['tax']} {fmt_rate(tl['rate'], lang)}", fmt_money(tl["amount"], lang, cur)))
    tot.append((f"<b>{_esc(t['total'])}</b>", f"<b>{fmt_money(spec['amount_total'], lang, cur)}</b>"))
    totals = (
        "<table class='totals'>"
        + "".join(
            f"<tr><td>{label if label.startswith('<b>') else _esc(label)}</td><td class='num'>{value}</td></tr>"
            for label, value in tot
        )
        + "</table>"
    )
    notes = []
    if spec.get("reverse_charge"):
        notes.append(t["rc"])
    elif spec["tax_lines"] and all(tl["rate"] == 0 for tl in spec["tax_lines"]):
        notes.append(t["import"])
    if spec.get("notes"):
        notes.append(spec["notes"])
    bank = f"{t['iban']} : {seller['iban']}" if seller.get("iban") else ""
    footer = (
        f"<p class='muted'>{' '.join(_esc(n) for n in notes)}</p>"
        f"<p class='muted'>{_esc(t['terms'])} {_esc(bank)}</p><p class='muted'>{_esc(t['thanks'])}</p>"
    )
    meta = (
        "<table style='width:auto'>"
        + "".join(f"<tr><td>{_esc(k)}</td><td>{_esc(v)}</td></tr>" for k, v in dates)
        + "</table>"
    )
    if layout == "classic":
        top = f"{seller_block}<h1>{_esc(title)}</h1>{meta}{buyer_block}"
    elif layout == "modern":
        inline = " · ".join(f"{_esc(k)} {_esc(v)}" for k, v in dates)
        top = (
            f"<div class='bar'>{_esc(seller['name'])}</div><p>{_esc(seller['address'])} · {_esc(vat_line)}</p>"
            f"<h1>{_esc(title)}</h1><p>{inline}</p>{buyer_block}"
        )
    elif layout == "compact":
        top = f"{buyer_block}{seller_block}<h1>{_esc(title)}</h1>{meta}"
    else:
        top = f"{seller_block}{buyer_block}<h1>{_esc(title)}</h1>{meta}"
    return (
        f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{top}{table}{totals}{footer}</body></html>"
    )


@lru_cache(maxsize=256)
def _pdf(html_text: str) -> bytes:
    import logging

    from weasyprint import HTML

    for name in ("fontTools", "weasyprint"):
        logging.getLogger(name).setLevel(logging.ERROR)  # font subsetting chatter
    return bytes(HTML(string=html_text).write_pdf())


def document_lines(spec: dict[str, Any]) -> list[str]:
    """The printed text of a bill, line by line (what a scan shows)."""
    import re

    text = document_html(spec)
    text = re.sub(r"</(tr|p|div|h1|thead|tbody|table)>|<br>", "\n", text)
    text = re.sub(r"</t[dh]>", "   ", text)
    text = re.sub(r"<style>.*?</style>", "", text, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return [ln.rstrip() for ln in text.split("\n") if ln.strip()]


@lru_cache(maxsize=64)
def _scan_cached(spec_json: str) -> bytes:
    import json

    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    spec = json.loads(spec_json)
    rnd = random.Random(spec["seed"] + 7)
    width, height = 1000, 1414
    img = Image.new("L", (width, height), 250)
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 17)
        bold = ImageFont.truetype("DejaVuSans-Bold.ttf", 26)
    except OSError:
        font = bold = ImageFont.load_default()
    y = 50
    for ln in document_lines(spec):
        f = bold if ln.strip().upper() == ln.strip() and len(ln.strip()) < 25 and ln.strip().isalpha() else font
        draw.text((60, y), ln.replace(" ", " "), fill=rnd.randint(10, 40), font=f)
        y += 34 if f is bold else 26
        if y > height - 60:
            break
    img = img.rotate(rnd.uniform(-2.5, 2.5), expand=False, fillcolor=245, resample=Image.Resampling.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(rnd.uniform(0.4, 1.1)))
    noise = Image.effect_noise((width, height), rnd.uniform(8, 18))
    img = Image.blend(img, noise, 0.08)
    img = img.resize((width * 4 // 5, height * 4 // 5))
    out = io.BytesIO()
    img.convert("L").save(out, "JPEG", quality=55, optimize=True)
    return out.getvalue()


def render(spec: dict[str, Any]) -> tuple[bytes, str, str]:
    """(content, media type, file name) of the bill as received: digital PDF, scan or image-only PDF."""
    import json

    kind = spec["kind"]
    if kind == "digital":
        return _pdf(document_html(spec)), "application/pdf", f"{spec['id']}.pdf"
    scan = _scan_cached(json.dumps(spec, sort_keys=True))
    if kind == "scan":
        return scan, "image/jpeg", f"{spec['id']}.jpg"
    img = base64.b64encode(scan).decode()
    page = f"<html><body style='margin:0'><img src='data:image/jpeg;base64,{img}' style='width:100%'></body></html>"
    return _pdf(page), "application/pdf", f"{spec['id']}.pdf"
