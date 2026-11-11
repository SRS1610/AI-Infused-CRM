"""Evaluation entries and aggregate scorers of the accounting AI features (``eval.entry`` / ``eval.scorer`` in
``ai/features.yaml``).

* ``account.bill_ocr``: each case renders its bill (``ai/evals/bills/<id>.json``: what is printed, the gold
  reading and the world it is read in), builds that world in the evaluation sandbox (the vendor, its bank
  account and its posted bills, as the system), then runs the production path **as an accounts-payable clerk**
  and counts every business record written before acceptance. Scores: header-field F1, line F1, account / tax
  accuracy on the matched lines, the proposal gate (one proposal carrying every auto-coded line, nothing
  written), company isolation of partner matching.
* ``account.anomaly``: the detector on labelled synthetic facts (duplicate recall, false-positive rate).
* ``account.cashflow_forecast``: back-test on simulated histories (MAPE of the weekly cash position against
  what actually followed the cut-off) and the numbers of the narrative against the engine's facts.
"""

from __future__ import annotations

import datetime as dt
import json
import statistics
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

from modules.account import ai_anomaly as A
from modules.account import ai_bill as B
from modules.account import ai_cashflow as C
from modules.account import ai_cashflow_synth as S
from modules.account import ai_ocr as O
from modules.account.chart import chart_ref
from tessera.ai.runtime import run_feature

BILLS = Path(__file__).parent / "ai" / "evals" / "bills"
WRITE_MODELS = ("account.move", "account.move_line", "contacts.partner", "account.bank_account", "product.product")
HEADER = O.Extraction.HEADER


@lru_cache(maxsize=256)
def load_bill(bill_id: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((BILLS / f"{bill_id}.json").read_text())
    return data


def _counts(env: Any) -> dict[str, int]:
    env.flush()
    out = {}
    for m in WRITE_MODELS:
        if m in env.registry.models:
            table = env.registry.models[m]._table
            out[m] = int(env.cr.scalar(f'SELECT count(*) FROM "{table}"') or 0)  # noqa: S608 - fixed names
    return out


def build_world(env: Any, company: Any, world: dict[str, Any]) -> int | None:
    """The vendor (or customer) as the company knows it; returns the partner id the reading should match."""
    vendor = world["vendor"]
    if not vendor.get("known"):
        return None
    sys_env = env.elevated("ai eval world").with_company(company).with_context(skip_ai_anomaly=True)
    other = sys_env.ref("core.main_company") if vendor.get("other_company") else None
    partner = sys_env["contacts.partner"].create(
        {
            "name": vendor["name"],
            "vat": vendor.get("vat"),
            "is_company": True,
            **({"company_id": other.ids[0]} if other is not None else {}),
        }
    )
    if vendor.get("iban"):
        sys_env["account.bank_account"].create(
            {
                "partner_id": partner.ids[0],
                "acc_number": vendor["iban"],
                **({"company_id": other.ids[0]} if other is not None else {}),
            }
        )
    mtype = "out_invoice" if vendor.get("customer") else "in_invoice"
    for past in world.get("history") or []:
        lines = []
        for ln in past["lines"]:
            acc = chart_ref(company, ln["account"])
            tax = chart_ref(company, ln["tax"]) if ln.get("tax") else None
            lines.append(
                (
                    "create",
                    {
                        "name": ln["description"],
                        "quantity": ln["quantity"],
                        "price_unit": ln["unit_price"],
                        "account_id": acc.ids[0] if acc else None,
                        "tax_ids": [("set", tax.ids if tax else [])],
                    },
                )
            )
        move = sys_env["account.move"].create(
            {
                "move_type": mtype,
                "partner_id": partner.ids[0],
                "invoice_date": dt.date.fromisoformat(past["date"]),
                "ref": f"HIST-{partner.ids[0]}",
                "invoice_line_ids": lines,
            }
        )
        move.with_context(skip_ai_anomaly=True).action_post()
    return None if other is not None else int(partner.ids[0])


def bill_ocr_entry(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    doc = load_bill(str(inputs["document"]))
    spec, gold, world = doc["spec"], doc["gold"], doc["world"]
    company = env.company
    expected_partner = build_world(env, company, world)
    content, mt, name = _render(spec)
    before = _counts(env)
    out = B.read_document(env.with_context(skip_ai_anomaly=True), content, mt, name)
    after = _counts(env)
    data = dict(out.get("data") or {})
    gold_lines = []
    for ln in gold["lines"]:
        acc = chart_ref(company, ln["account"])
        tax = chart_ref(company, ln["tax"]) if ln.get("tax") else None
        gold_lines.append({**ln, "account_id": acc.ids[0] if acc else None, "tax_ids": tax.ids if tax else []})
    proposals = out.get("proposals") or []
    payload_lines = []
    if proposals:
        args = proposals[0]["payload"].get("args") or {}
        payload_lines = (args.get("values") or {}).get("invoice_line_ids") or []
    data.update(
        writes_before_accept=sum(after[m] - before.get(m, 0) for m in after),
        expected_partner_id=expected_partner,
        gold_lines=gold_lines,
        payload_lines=len(payload_lines),
        payload_coded=sum(1 for c in payload_lines if c[1].get("account_id")),
    )
    data.pop("extraction_text", None)
    return {
        "data": data,
        "text": "",
        "proposals": len(proposals),
        "status": out.get("status", "ok"),
        "flags": out.get("flags", []),
    }


def _render(spec: dict[str, Any]) -> tuple[bytes, str, str]:
    from modules.account.ai.bills.render import render

    return render(spec)


def _same(field: str, got: Any, exp: Any) -> bool:
    if got in (None, "") or exp in (None, ""):
        return False
    if field in ("amount_untaxed", "amount_tax", "amount_total"):
        return abs(float(got) - float(exp)) <= 0.01
    if field in ("partner_vat", "iban"):
        return O.norm_vat(str(got)) == O.norm_vat(str(exp))
    if field == "partner_name":
        return " ".join(O.words(str(got))) == " ".join(O.words(str(exp)))
    return str(got).strip().upper() == str(exp).strip().upper()


def _f1(tp: int, fp: int, fn: int) -> float:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    return 2 * p * r / (p + r) if p + r else 0.0


def _line_match(pred: dict[str, Any], gold: dict[str, Any]) -> bool:
    desc = SequenceMatcher(None, " ".join(O.words(pred["description"])), " ".join(O.words(gold["description"]))).ratio()
    rate_p = pred.get("tax_rate") if pred.get("tax_rate") is not None else -1
    return (
        desc >= 0.8
        and abs(float(pred["quantity"]) - float(gold["quantity"])) < 1e-6
        and abs(float(pred["unit_price"]) - float(gold["unit_price"])) <= 0.01
        and abs(float(pred["amount"]) - float(gold["amount"])) <= 0.01
        and abs(float(rate_p) - float(gold["tax_rate"])) < 1e-6
    )


def bill_ocr_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    h_tp = h_fp = h_fn = 0
    l_tp = l_fp = l_fn = 0
    acc_ok = tax_ok = both_ok = coded = 0
    gate: list[float] = []
    iso: list[float] = []
    vendor: list[float] = []
    mtype: list[float] = []
    review = fallback = 0
    for r in results:
        exp = r["_case"]["expected"]
        data = r.get("data") if isinstance(r.get("data"), dict) else {}
        header = (data.get("extraction") or {}) if data else {}
        for f in HEADER:
            g, p = exp.get(f), header.get(f)
            if g not in (None, "") and _same(f, p, g):
                h_tp += 1
            else:
                if g not in (None, ""):
                    h_fn += 1
                if p not in (None, ""):
                    h_fp += 1
        pred = list(data.get("lines") or [])
        golds = list(data.get("gold_lines") or exp.get("lines") or [])
        used: set[int] = set()
        for g in golds:
            k = next((i for i, p in enumerate(pred) if i not in used and _line_match(p, g)), None)
            if k is None:
                l_fn += 1
                continue
            used.add(k)
            l_tp += 1
            coded += 1
            p = pred[k]
            a = p.get("account_id") == g.get("account_id")
            t = sorted(p.get("tax_ids") or []) == sorted(g.get("tax_ids") or [])
            acc_ok += a
            tax_ok += t
            both_ok += a and t
        l_fp += len(pred) - len(used)
        if pred:
            ok = (
                r.get("proposals") == 1
                and data.get("payload_lines") == len(pred)
                and data.get("writes_before_accept") == 0
            )
            gate.append(1.0 if ok else 0.0)
        elif data:
            gate.append(1.0 if data.get("writes_before_accept") == 0 and not r.get("proposals") else 0.0)
        if "isolation" in r["_case"].get("tags", []):
            iso.append(1.0 if (data.get("partner") or {}).get("method") == "new" else 0.0)
        if data.get("expected_partner_id") is not None:
            vendor.append(1.0 if (data.get("partner") or {}).get("id") == data["expected_partner_id"] else 0.0)
        if data:
            mtype.append(1.0 if data.get("move_type") == exp.get("move_type") else 0.0)
            review += bool(data.get("needs_review"))
            fallback += data.get("method") != "text"
    n = len(results) or 1

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    return {
        "header_f1": _f1(h_tp, h_fp, h_fn),
        "line_f1": _f1(l_tp, l_fp, l_fn),
        "account_accuracy": acc_ok / coded if coded else 0.0,
        "tax_accuracy": tax_ok / coded if coded else 0.0,
        "coding_accuracy": both_ok / coded if coded else 0.0,
        "proposal_gate": mean(gate) if gate else 0.0,
        "isolation": mean(iso) if iso else 0.0,
        "vendor_match_accuracy": mean(vendor),
        "move_type_accuracy": mean(mtype),
        "needs_review_rate": review / n,
        "model_reading_rate": fallback / n,
    }


# ----------------------------------------------------------------------------- anomalies


BOILER = (
    "{vendor} SARL 14 rue des Entrepreneurs 69007 Lyon France VAT FR40303265045 Bill to My Company Europe 8 avenue "
    "de l Opera 75001 Paris Description quantity unit price amount maintenance of the production line and on site "
    "support as agreed in the framework contract payment by bank transfer within thirty days late payment penalties "
    "three times the legal interest rate fixed indemnity forty euros for recovery costs thank you for your business"
)


def doc_text(vendor: str, number: str, day: dt.date, amount: float) -> str:
    """The text of a synthetic bill as OCR would return it (the anomaly cases' default document text)."""
    return f"Invoice {number} date {day.isoformat()} " + BOILER.format(vendor=vendor) + f" total {amount:.2f} EUR"


def _bill(d: dict[str, Any]) -> A.Bill:
    day = dt.date.fromisoformat(d["date"])
    text = d.get("text")
    if text is None:
        text = doc_text(str(d["partner"]), d.get("number") or "", day, float(d["amount"]))
    return A.Bill(d.get("id"), str(d["partner"]), d.get("number") or "", day, float(d["amount"]), text,
                  d.get("label") or "")  # fmt: skip


def anomaly_entry(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    A.check_enabled(env, A.get_feature(env, A.FEATURE))
    v = inputs.get("vendor") or {}
    vendor = A.Vendor(
        known_ibans=[O.norm_iban(i) for i in v.get("known_ibans", [])],
        bank_changed_days=v.get("bank_changed_days"),
        has_older_bills=bool(v.get("has_older_bills")),
        document_iban=v.get("document_iban"),
    )
    found = A.detect(_bill(inputs["bill"]), [_bill(o) for o in inputs.get("others", [])], vendor,
                     [float(x) for x in inputs.get("company_amounts", [])])  # fmt: skip
    data = {"kinds": sorted({f.kind for f in found}), "findings": [f.to_dict() for f in found]}
    return {"data": data, "text": json.dumps(data, sort_keys=True), "proposals": 0, "status": "ok", "flags": []}


def anomaly_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    dup_hit = dup_total = clean_flagged = clean_total = 0
    kind_tp: dict[str, int] = {}
    kind_fp: dict[str, int] = {}
    explained = flagged = 0
    for r in results:
        expected = set(r["_case"]["expected"].get("kinds") or [])
        got = set((r.get("data") or {}).get("kinds") or [])
        if expected & set(A.DUPLICATE_KINDS):
            dup_total += 1
            dup_hit += bool(got & set(A.DUPLICATE_KINDS))
        if not expected:
            clean_total += 1
            clean_flagged += bool(got)
        for k in got:
            group = k if k not in A.DUPLICATE_KINDS else "duplicate"
            exp_groups = {e if e not in A.DUPLICATE_KINDS else "duplicate" for e in expected}
            (kind_tp if group in exp_groups else kind_fp)[group] = (kind_tp if group in exp_groups else kind_fp).get(
                group, 0
            ) + 1
        for f in (r.get("data") or {}).get("findings") or []:
            flagged += 1
            explained += bool(f.get("explanation"))
    out = {
        "duplicate_recall": dup_hit / dup_total if dup_total else 0.0,
        "false_positive_rate": clean_flagged / clean_total if clean_total else 0.0,
        "explained": explained / flagged if flagged else 1.0,
    }
    for group in sorted(set(kind_tp) | set(kind_fp)):
        tp, fp = kind_tp.get(group, 0), kind_fp.get(group, 0)
        out[f"precision_{group}"] = tp / (tp + fp) if tp + fp else 0.0
    return out


# ----------------------------------------------------------------------------- cash flow


def cashflow_entry(env: Any, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    C.ensure_enabled(env)
    params = inputs["scenario"]
    data, _actual = S.simulate(params)
    result = C.forecast(data, int(params.get("weeks", 8)))
    company = env.company or env.ref("core.main_company")
    run = run_feature(
        env, C.FEATURE, {"weeks": int(params.get("weeks", 8))}, **C.narrative_kwargs(result, company.ids[0])
    )
    out = run.data if isinstance(run.data, dict) else {}
    narrative = str(out.get("narrative") or C.plain_summary(result))
    facts = C.facts_text(result)
    payload = {
        "positions": [w["position"] for w in result["weeks"]],
        "flat": [data.opening] * len(result["weeks"]),
        "narrative": narrative,
        "narrative_source": out.get("narrative_source", "facts"),
        "foreign_in_shown": C.foreign_numbers(narrative, facts),
        "rejected": bool(out.get("foreign_numbers")),
    }
    return {
        "data": payload,
        "text": narrative,
        "proposals": len(run.proposals),
        "status": run.run.status,
        "flags": run.flags,
    }


def cashflow_scorer(results: list[dict[str, Any]]) -> dict[str, float]:
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    errs = [C.mape(r["data"]["positions"], r["_case"]["expected"]["actual"]) for r in ok]
    flat = [C.mape(r["data"]["flat"], r["_case"]["expected"]["actual"]) for r in ok]
    shown = [0.0 if r["data"]["foreign_in_shown"] else 1.0 for r in ok]
    return {
        "mape": statistics.mean(errs) if errs else 1.0,
        "mape_median": statistics.median(errs) if errs else 1.0,
        "mape_flat_baseline": statistics.mean(flat) if flat else 1.0,
        "numbers_from_engine": statistics.mean(shown) if shown else 0.0,
        "narratives_replaced": float(sum(1 for r in ok if r["data"]["rejected"])),
        "narratives_from_model": float(sum(1 for r in ok if r["data"]["narrative_source"] == "model")),
    }
