"""Scenarios of the ``account_tax.prefiling`` evaluation (``ai/build_evals.py`` writes the cases).

:func:`build` books five quarters of ordinary activity for the EU demo company (French chart) in 2040-2041 —
domestic sales at 20 %, an EU business customer (intra-EU, 0 %), an EU consumer (20 %), an export customer (0 %),
domestic and EU purchases (reverse charge) — with amounts drawn from the case's seed, then seeds the case's
anomaly in the last quarter (Q2 2041) and returns its tax return. The entry reviews the return as the case's
user; recall counts the seeded anomalies found by a finding that points at the seeded document (or grid).
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

from modules.account.chart import chart_ref
from modules.account_tax import ai_prefiling as A

D = dt.date
PERIODS = [(D(2040, 4, 1), D(2040, 6, 30)), (D(2040, 7, 1), D(2040, 9, 30)), (D(2040, 10, 1), D(2040, 12, 31)),
           (D(2041, 1, 1), D(2041, 3, 31)), (D(2041, 4, 1), D(2041, 6, 30))]  # fmt: skip
CURRENT = PERIODS[-1]
# anomaly → (codes that count as finding it, whether the finding must list the seeded document)
EXPECTED: dict[str, tuple[list[str], bool]] = {
    "rate_domestic_10": (["rate_outlier_for_country", "unusual_tax_for_country"], True),
    "rate_domestic_0": (["rate_outlier_for_country", "unusual_tax_for_country"], True),
    "rate_consumer_0": (["rate_outlier_for_country", "unusual_tax_for_country"], True),
    "b2b_domestic_vat": (["fiscal_position_mismatch", "rate_outlier_for_country"], True),
    "export_with_vat": (["fiscal_position_mismatch", "rate_outlier_for_country"], True),
    "swing_up": (["grid_swing"], False),
    "swing_down": (["grid_swing"], False),
    "swing_vanished": (["grid_swing"], False),
    "swing_new": (["grid_swing"], False),
    "rc_without_vat": (["reverse_charge_without_vat"], True),
    "draft": (["draft_entries"], True),
    "gridless_tax": (["tax_lines_without_grid", "taxes_without_grids", "report_ledger_mismatch"], False),
    "manual_tax_entry": (["report_ledger_mismatch"], False),
}


def _partner(env: Any, name: str, country: str, vat: str | None = None, **vals: Any) -> Any:
    return env["contacts.partner"].create(
        {"name": name, "is_company": True, "country_id": env.ref(f"core.country_{country}").ids[0], "vat": vat, **vals}
    )


def _doc(env: Any, partner: Any, day: dt.date, amount: float, tax: Any, *, kind: str = "out_invoice",
         post: bool = True, **vals: Any) -> Any:  # fmt: skip
    move = env["account.move"].create(
        {
            "move_type": kind,
            "partner_id": partner.ids[0],
            "invoice_date": day,
            "date": day,
            "invoice_line_ids": [
                (
                    "create",
                    {"name": "Goods", "quantity": 1, "price_unit": round(amount, 2), "tax_ids": [("set", tax.ids)]},
                )
            ],
            **vals,
        }
    )
    if post:
        move.action_post()
    return move


def build(env: Any, scenario: dict[str, Any]) -> tuple[Any, list[int]]:
    """(the tax return of the current quarter, ids of the seeded documents)."""
    e = env.elevated("ai eval scenario")
    company = e.company
    rng = random.Random(scenario["seed"])  # noqa: S311 - reproducible synthetic data
    tax = {k: chart_ref(company, k) for k in ("sale_vat_20", "sale_vat_10", "sale_vat_0_export", "sale_vat_0_eu",
                                               "purchase_vat_20", "purchase_vat_0_import")}  # fmt: skip
    label = scenario.get("name_suffix", "")
    fr = [_partner(e, f"Client Lyon{label}", "fr"), _partner(e, f"Client Lille{label}", "fr")]
    de_b2b = _partner(e, f"Kunde Berlin GmbH{label}", "de", "DE123456789")
    be_b2c = _partner(e, f"Particulier Bruxelles{label}", "be")
    us = _partner(e, f"Customer Boston{label}", "us")
    fr_supplier = _partner(e, f"Fournisseur Nantes{label}", "fr")
    de_supplier = _partner(e, f"Lieferant Hamburg{label}", "de", "DE987654321")
    anomaly = scenario.get("anomaly")
    seeded: list[int] = []
    for start, _end in PERIODS:
        current = start == CURRENT[0]
        day = start + dt.timedelta(days=rng.randint(5, 50))
        level = scenario.get("level", 1.0) * rng.uniform(0.85, 1.15)
        sales_factor = {"swing_up": 4.0, "swing_down": 0.15}.get(anomaly, 1.0) if current else 1.0
        for p in fr:
            _doc(e, p, day, rng.uniform(1800, 2600) * level * sales_factor, tax["sale_vat_20"])
        _doc(e, de_b2b, day, rng.uniform(900, 1300) * level, tax["sale_vat_20"])
        _doc(e, be_b2c, day, rng.uniform(300, 500) * level, tax["sale_vat_20"])
        _doc(e, us, day, rng.uniform(600, 900) * level, tax["sale_vat_20"])
        if not (current and anomaly == "swing_vanished"):
            _doc(e, fr_supplier, day, rng.uniform(1200, 1600) * level, tax["purchase_vat_20"], kind="in_invoice")
        _doc(e, de_supplier, day, rng.uniform(700, 900) * level, tax["purchase_vat_20"], kind="in_invoice")
    day = CURRENT[0] + dt.timedelta(days=60)
    if anomaly == "rate_domestic_10":
        seeded.append(_doc(e, fr[0], day, 640.0, tax["sale_vat_10"]).ids[0])
    elif anomaly == "rate_domestic_0":
        seeded.append(_doc(e, fr[1], day, 880.0, tax["sale_vat_0_export"]).ids[0])
    elif anomaly == "rate_consumer_0":
        seeded.append(_doc(e, be_b2c, day, 420.0, tax["sale_vat_0_eu"]).ids[0])
    elif anomaly == "b2b_domestic_vat":
        other = _partner(e, f"Kunde München AG{label}", "de", "DE111222333")
        seeded.append(_doc(e, other, day, 1500.0, tax["sale_vat_20"], fiscal_position_id=None).ids[0])
    elif anomaly == "export_with_vat":
        seeded.append(_doc(e, us, day, 730.0, tax["sale_vat_20"], fiscal_position_id=None).ids[0])
    elif anomaly == "swing_new":
        importer = _partner(e, f"Supplier Austin{label}", "us")
        seeded.append(_doc(e, importer, day, 6000.0, tax["purchase_vat_20"], kind="in_invoice").ids[0])
    elif anomaly == "rc_without_vat":
        no_vat = _partner(
            e, f"Lieferant ohne USt{label}", "de", property_account_position_id=chart_ref(company, "fp_eu_b2b").ids[0]
        )
        seeded.append(_doc(e, no_vat, day, 900.0, tax["purchase_vat_20"], kind="in_invoice").ids[0])
    elif anomaly == "draft":
        seeded.append(_doc(e, fr[0], day, 1100.0, tax["sale_vat_20"], post=False).ids[0])
    elif anomaly == "gridless_tax":
        payable = e["account.account"].search([("code", "=", "2110"), ("company_id", "=", company.ids[0])], limit=1)
        reps = [("create", {"document_type": doc, "repartition_type": k, "factor_percent": 100.0,
                            **({"account_id": payable.ids[0]} if k == "tax" else {})})
                for doc in ("invoice", "refund") for k in ("base", "tax")]  # fmt: skip
        gridless = e["account.tax"].create(
            {"name": f"Local levy 7%{label}", "amount": 7, "type_tax_use": "sale", "company_id": company.ids[0],
             "repartition_line_ids": reps}
        )  # fmt: skip
        seeded.append(_doc(e, fr[1], day, 1000.0, gridless).ids[0])
    elif anomaly == "manual_tax_entry":
        payable = e["account.account"].search([("code", "=", "2110"), ("company_id", "=", company.ids[0])], limit=1)
        other = e["account.account"].search(
            [("account_type", "=", "expense"), ("company_id", "=", company.ids[0])], limit=1
        )
        journal = e["account.journal"].search([("type", "=", "general"), ("company_id", "=", company.ids[0])], limit=1)
        move = e["account.move"].create(
            {"move_type": "entry", "journal_id": journal.ids[0], "date": day, "ref": "VAT correction",
             "line_ids": [("create", {"name": "VAT", "account_id": payable.ids[0], "balance": -350.0}),
                          ("create", {"name": "VAT", "account_id": other.ids[0], "balance": 350.0})]}
        )  # fmt: skip
        move.action_post()
        seeded.append(move.ids[0])
    ret = e["account.tax_return"].create({"company_id": company.ids[0], "date_from": CURRENT[0], "date_to": CURRENT[1]})
    return ret, seeded


def entry(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    ret, seeded = build(env, inputs["scenario"])
    out = A.review(env, env["account.tax_return"].browse(ret.ids))
    anomaly = inputs["scenario"].get("anomaly")
    found = False
    rank = None
    if anomaly:
        codes, needs_doc = EXPECTED[anomaly]
        for item in out["items"]:
            if item["code"] in codes and (not needs_doc or set(seeded) & set(item["res_ids"])):
                found = True
                rank = item["rank"] if rank is None else min(rank, item["rank"])
    data = {
        "codes": [i["code"] for i in out["items"]],
        "statistical": [i["code"] for i in out["items"] if i["code"] in A.STATISTICAL],
        "found": found,
        "rank": rank,
        "explained": all(i["why"] and i["action"] for i in out["items"]),
    }
    return {"text": "", "data": data, "proposals": 0, "status": "ok", "flags": []}


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``recall``: seeded anomalies found by a finding pointing at the seeded document or grid (≥ 0.9);
    ``top3``: of those, the share ranked in the first three; ``clean_flag_rate``: clean periods with a
    statistical flag (information); ``explained``: findings with an explanation and an action."""
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    seeded = [r for r in ok if r["_case"]["expected"].get("anomaly")]
    clean = [r for r in ok if not r["_case"]["expected"].get("anomaly")]
    found = [r for r in seeded if r["data"]["found"]]
    out = {
        "recall": len(found) / len(seeded) if seeded else 1.0,
        "top3": sum(1 for r in found if (r["data"]["rank"] or 99) <= 3) / len(found) if found else 0.0,
        "explained": sum(1 for r in ok if r["data"]["explained"]) / len(ok),
    }
    if clean:
        out["clean_flag_rate"] = sum(1 for r in clean if r["data"]["statistical"]) / len(clean)
    return out
