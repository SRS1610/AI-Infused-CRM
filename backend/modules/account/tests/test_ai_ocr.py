"""Deterministic document reading (``modules.account.ai_ocr``) and the synthetic bill corpus — no database."""

from __future__ import annotations

import datetime as dt

import pytest

from modules.account import ai_ocr as O
from modules.account.ai.bills import corpus, render


@pytest.mark.parametrize(
    ("token", "value"),
    [
        ("1 234,56", 1234.56),
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
        ("1'234.50", 1234.5),
        ("12,5", 12.5),
        ("0,42", 0.42),
        ("1.234", 1234.0),
        ("-42.00", -42.0),
        ("£1,250.00", 1250.0),
        ("abc", None),
    ],
)
def test_parse_number_conventions(token: str, value: float | None) -> None:
    assert O.parse_number(token) == value


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("14/03/2026", dt.date(2026, 3, 14)),
        ("14.03.2026", dt.date(2026, 3, 14)),
        ("2026-03-14", dt.date(2026, 3, 14)),
        ("14 mars 2026", dt.date(2026, 3, 14)),
        ("14. marz 2026", dt.date(2026, 3, 14)),
        ("14 de marzo de 2026", dt.date(2026, 3, 14)),
        ("march 14, 2026", dt.date(2026, 3, 14)),
        ("31/02/2026", None),
    ],
)
def test_parse_date_four_languages(text: str, day: dt.date | None) -> None:
    assert O.parse_date(text)[0] == day


def test_row_solver_needs_quantity_times_price() -> None:
    assert O.solve_row(
        ["3", "1", "250,00", "5,5", "%", "3", "750,00"], ["quantity", "unit_price", "tax_rate", "amount"]
    ) == (
        3.0,
        1250.0,
        3750.0,
        5.5,
    )
    assert O.solve_row(["2", "10.00", "25.00"], ["quantity", "unit_price", "amount"]) is None
    assert O.solve_row(["4", "2.50", "10.00"], []) == (4.0, 2.5, 10.0, None)
    assert O.amount_of("TVA 20 % 42,00 €") == 42.0
    assert O.amount_of("Total TTC 4 208,25 €") == 4208.25
    assert O.rate_of("MwSt. 19 % 12,00") == 19.0


def test_identifiers_and_kerning() -> None:
    assert O.iban_valid("FR76 3000 6000 0112 3456 7890 189") and not O.iban_valid("FR76 3000 6000 0112 3456 7890 188")
    assert O.norm_vat("fr 40 303 265 045") == "FR40303265045"
    assert O.KERNING_SPLIT.sub(r"\1", "T oner cartridge and A brochure") == "Toner cartridge and A brochure"


def test_corpus_is_deterministic_and_balanced() -> None:
    a, b = corpus.draw(), corpus.draw()
    assert [x["spec"]["invoice_number"] for x in a] == [x["spec"]["invoice_number"] for x in b]
    kinds = {k: sum(1 for x in a if x["spec"]["kind"] == k) for k in ("digital", "scan", "image_pdf")}
    assert len(a) >= 100 and kinds["scan"] + kinds["image_pdf"] >= 30
    assert {x["spec"]["lang"] for x in a} == {"en", "fr", "de", "es"}
    assert {x["spec"]["layout"] for x in a} == set(corpus.LAYOUTS)
    for x in a:
        spec = x["spec"]
        assert round(sum(ln["amount"] for ln in spec["lines"]), 2) == spec["amount_untaxed"]
        assert round(spec["amount_untaxed"] + spec["amount_tax"], 2) == spec["amount_total"]


@pytest.mark.parametrize("index", [0, 1, 2, 3, 13, 21, 22, 108, 110])
def test_digital_bills_are_read_exactly(index: int) -> None:
    """Every layout and language: the text layer gives the gold header and lines."""
    bill = corpus.draw()[index]
    spec, gold = bill["spec"], bill["gold"]
    content, media, _name = render.render(spec)
    assert media == "application/pdf"
    text = O.text_layer(content)
    ex = O.parse_text(text, company_names=[corpus.COMPANY["name"]], company_vats=[])
    header = ex.header()
    for key in ("partner_name", "partner_vat", "invoice_number", "invoice_date", "due_date", "currency"):
        assert header[key] == gold[key], key
    assert header["amount_total"] == pytest.approx(gold["amount_total"])
    assert [(ln.description, ln.quantity, ln.unit_price, ln.amount, ln.tax_rate) for ln in ex.lines] == [
        (ln["description"], ln["quantity"], ln["unit_price"], ln["amount"], ln["tax_rate"]) for ln in gold["lines"]
    ]
    assert ex.consistent() and not ex.missing()
    assert ex.seller_is_company == (spec["move_type"] == "out_invoice")


def test_scans_and_image_pdfs_have_no_text_layer() -> None:
    bills = corpus.draw()
    scan = next(b for b in bills if b["spec"]["kind"] == "scan")
    image_pdf = next(b for b in bills if b["spec"]["kind"] == "image_pdf")
    content, media, _ = render.render(scan["spec"])
    assert media == "image/jpeg" and content[:3] == b"\xff\xd8\xff" and len(content) < 400_000
    assert O.text_layer(content) == ""
    content, media, _ = render.render(image_pdf["spec"])
    assert media == "application/pdf" and O.text_layer(content) == ""
    assert O.text_layer(b"%PDF-1.4 broken") == ""


def test_model_answer_is_normalised_and_grounded() -> None:
    data = {
        "partner_name": "Acme SARL",
        "partner_vat": "fr 40 303 265 045",
        "iban": "FR76 3000 6000 0112 3456 7890 188",  # invalid check digits: dropped
        "invoice_number": "F-1",
        "invoice_date": "14/03/2026",
        "due_date": None,
        "currency": "eur",
        "lines": [{"description": "Paper", "quantity": 2, "unit_price": 5.5, "tax_rate": 20, "amount": None}, "junk"],
        "tax_lines": [{"rate": 20, "amount": 2.2}],
        "amount_untaxed": 11,
        "amount_tax": "2,20",
        "amount_total": 13.2,
        "document_type": "credit_note",
        "reverse_charge": False,
        "issued_by_us": False,
    }
    ex = O.from_model(data, method="model", text="")
    assert ex.partner_vat == "FR40303265045" and ex.iban is None and ex.currency == "EUR"
    assert ex.invoice_date == dt.date(2026, 3, 14) and ex.document_type == "credit_note"
    assert ex.lines[0].amount == 11.0 and ex.amount_tax == 2.2 and ex.consistent()
    assert O.grounding(ex, "Paper 2 5,50 11,00 total 13,20 tax 2,20") == []
    assert O.grounding(ex, "Paper 2 5,50 11,00 total 99,00") == [2.2, 13.2]
    assert O.from_model("not a dict", method="vision").lines == []
