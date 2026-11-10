"""EU golden fixture: sales, credit note, intra-EU supply, export, purchases, reverse charge and import."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account.chart import chart_ref
from modules.account.tests.common import invoice, partner
from modules.account_reports.tests.common import isolated

D = dt.date
Q2 = (D(2031, 4, 1), D(2031, 6, 30))
EXPECTED_EU = {
    "G01": 1300.0,
    "G02": 210.0,
    "G03": 800.0,
    "G04": 300.0,
    "G05": 400.0,
    "G06": 80.0,
    "G07": 1000.0,
    "G08": 200.0,
    "G09": 200.0,
    "G10": 250.0,
    "TAX_DUE": 410.0,
    "TAX_DEDUCTIBLE": 280.0,
    "NET": 130.0,
}


def tax(env: Any, tid: str) -> Any:
    return chart_ref(env.company, tid)


def line(env: Any, price: float, tid: str, **vals: Any) -> dict[str, Any]:
    return {"quantity": 1, "price_unit": price, "tax_ids": [("set", tax(env, tid).ids)], **vals}


def eu_golden(env: Any) -> dict[str, Any]:
    e = isolated(env, "Golden SARL", country="fr", currency="EUR")
    fr, de, us = (e.ref(f"core.country_{c}").ids[0] for c in ("fr", "de", "us"))
    domestic = partner(e, "Client Paris", country_id=fr)
    eu_customer = partner(e, "Kunde Berlin", country_id=de, vat="DE123456789")
    world_customer = partner(e, "Customer Boston", country_id=us)
    supplier = partner(e, "Fournisseur Lyon", country_id=fr)
    eu_supplier = partner(e, "Lieferant Hamburg", country_id=de, vat="DE987654321")
    world_supplier = partner(e, "Supplier Austin", country_id=us)
    day = D(2031, 4, 15)
    docs = {
        "sale": invoice(e, domestic, [line(e, 1000.0, "sale_vat_20")], invoice_date=day),
        "sale_10": invoice(e, domestic, [line(e, 500.0, "sale_vat_10")], invoice_date=day),
        "refund": invoice(e, domestic, [line(e, 200.0, "sale_vat_20")], move_type="out_refund", invoice_date=day),
        "intra_eu": invoice(e, eu_customer, [line(e, 800.0, "sale_vat_20")], invoice_date=day),
        "export": invoice(e, world_customer, [line(e, 300.0, "sale_vat_20")], invoice_date=day),
        "bill": invoice(
            e, supplier, [line(e, 400.0, "purchase_vat_20")], move_type="in_invoice", invoice_date=day, date=day
        ),
        "reverse_charge": invoice(
            e, eu_supplier, [line(e, 1000.0, "purchase_vat_20")], move_type="in_invoice", invoice_date=day, date=day
        ),
        "import": invoice(
            e, world_supplier, [line(e, 250.0, "purchase_vat_20")], move_type="in_invoice", invoice_date=day, date=day
        ),
    }
    return {"env": e, "company": e.company, "docs": docs, "partners": {"eu_supplier": eu_supplier}}
