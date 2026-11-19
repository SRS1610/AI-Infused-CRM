"""``account_einvoice.error_explain`` — plain-language explanations of e-invoice validation findings (2.6).

``account.edi_document.errors`` holds the structured findings of the vendored validators (XSD, CEN EN 16931,
Peppol BIS 3): rule id, flag, location (XPath), message. For each rule (repeated findings are grouped):

* **which field to fix** is decided here, deterministically (:func:`attribute`): the rule catalogue
  (``RULES``) for rules reported on the whole document, otherwise the finding's location — the party
  (``AccountingCustomerParty`` → the customer, ``AccountingSupplierParty`` → the company), the invoice line,
  the tax category, the totals or the payment means — and its last element (``EndpointID`` → Peppol address,
  ``PartyTaxScheme/CompanyID`` → VAT number, ``Country/IdentificationCode`` → country…). XSD messages name the
  element. The result carries a link target (model, record id, field);
* **the explanation and the fix hint** are written by the mid-tier model from the findings and the invoice's
  facts; a sentence quoting a figure that is in neither is removed, a rule the model invents is ignored, and a
  rule it skips gets the catalogue's own wording. Nothing is written.
"""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends

from tessera.ai import Source
from tessera.ai.errors import AIError
from tessera.ai.facts import audit_run, ensure_enabled, ground_text
from tessera.ai.runtime import HANDLERS, handler, run_feature
from tessera.api.deps import RequestContext, authenticated
from tessera.api.registry import register_router
from tessera.exceptions import MissingError, UserError
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

FEATURE = "account_einvoice.error_explain"

# rules reported on the whole document (or whose location does not say which field): rule → target
RULES: dict[str, str] = {
    "BR-01": "invoice.move_type", "BR-02": "invoice.name", "BR-03": "invoice.invoice_date",
    "BR-04": "invoice.move_type", "BR-05": "invoice.currency_id", "BR-06": "company.name", "BR-07": "partner.name",
    "BR-08": "company.street", "BR-09": "company.country_id", "BR-10": "partner.street", "BR-11": "partner.country_id",
    "BR-16": "invoice.invoice_line_ids", "BR-25": "line.name", "BR-26": "line.price_unit",
    "BR-CO-09": "partner.vat", "BR-CO-10": "invoice.invoice_line_ids", "BR-CO-13": "invoice.invoice_line_ids",
    "BR-CO-14": "invoice.invoice_line_ids", "BR-CO-15": "invoice.invoice_line_ids",
    "BR-CO-16": "invoice.invoice_line_ids", "BR-CO-17": "invoice.invoice_line_ids",
    "BR-CO-25": "invoice.invoice_date_due", "BR-61": "bank.acc_number", "BR-CL-01": "invoice.move_type",
    "BR-CL-03": "invoice.currency_id", "BR-CL-04": "invoice.currency_id", "BR-CL-16": "bank.acc_number",
    "BR-CL-23": "line.product_uom_id", "BR-CL-18": "tax.edi_tax_category", "BR-CL-14": "partner.country_id",
    "BR-CL-11": "company.company_registry", "BR-CL-25": "partner.peppol_eas",
    "BR-S-01": "tax.edi_tax_category", "BR-S-02": "company.vat", "BR-S-05": "line.tax_ids",
    "BR-S-08": "invoice.invoice_line_ids", "BR-E-01": "tax.edi_tax_category", "BR-E-02": "company.vat",
    "BR-E-08": "invoice.invoice_line_ids", "BR-E-09": "invoice.invoice_line_ids",
    "BR-E-10": "tax.edi_exemption_reason", "BR-AE-02": "partner.vat", "BR-AE-10": "tax.edi_exemption_reason",
    "BR-IC-02": "partner.vat", "BR-IC-10": "tax.edi_exemption_reason", "BR-IC-11": "invoice.invoice_date",
    "BR-IC-12": "partner.country_id", "BR-G-02": "company.vat", "BR-G-10": "tax.edi_exemption_reason",
    "BR-Z-02": "company.vat", "BR-O-02": "tax.edi_tax_category",
    "PEPPOL-EN16931-R003": "invoice.ref", "PEPPOL-EN16931-R010": "partner.peppol_endpoint",
    "PEPPOL-EN16931-R020": "company.peppol_endpoint", "PEPPOL-EN16931-R120": "invoice.invoice_line_ids",
    "PEPPOL-EN16931-R121": "invoice.invoice_line_ids", "PEPPOL-EN16931-R061": "invoice.invoice_date_due",
    "PEPPOL-EN16931-CL007": "invoice.currency_id", "PEPPOL-EN16931-CL008": "partner.peppol_eas",
    "PEPPOL-EN16931-P0100": "invoice.move_type", "PEPPOL-EN16931-P0101": "invoice.move_type",
    "TESSERA-DATA": "partner.peppol_endpoint", "ACCESS-POINT": "company.peppol_endpoint",
}  # fmt: skip
# rules whose meaning is the field's *code* rather than its value
SCHEME_RULES = {"BR-CL-25", "PEPPOL-EN16931-CL008"}
# last element of a location (or the element an XSD message names) → field, by context
LEAF_FIELDS: dict[str, str] = {
    "EndpointID": "peppol_endpoint", "RegistrationName": "name", "Name": "name", "StreetName": "street",
    "AdditionalStreetName": "street2", "CityName": "city", "PostalZone": "zip", "IdentificationCode": "country_id",
    "ElectronicMail": "email", "Telephone": "phone",
}  # fmt: skip
INVOICE_LEAVES = {
    "IssueDate": "invoice.invoice_date", "DueDate": "invoice.invoice_date_due", "ID": "invoice.name",
    "InvoiceTypeCode": "invoice.move_type", "CreditNoteTypeCode": "invoice.move_type",
    "DocumentCurrencyCode": "invoice.currency_id", "BuyerReference": "invoice.ref", "Note": "invoice.narration",
}  # fmt: skip
STEP = re.compile(r"\*:([A-Za-z]+)(?:\[namespace-uri\(\)='[^']*'\])?\[(\d+)\]")


def _steps(location: str) -> list[tuple[str, int]]:
    return [(m.group(1), int(m.group(2))) for m in STEP.finditer(location or "")]


def attribute(finding: dict[str, Any]) -> str:
    """The field to fix for one finding: ``<kind>.<field>`` (kind: partner, company, invoice, line, tax, bank)."""
    rule = str(finding.get("rule_id") or "")
    steps = [name for name, _i in _steps(str(finding.get("location") or ""))]
    party = (
        "partner" if "AccountingCustomerParty" in steps else "company" if "AccountingSupplierParty" in steps else None
    )
    leaf = steps[-1] if steps else None
    if rule == "XSD":
        m = re.search(r"\}([A-Za-z]+)'", str(finding.get("message") or ""))
        return INVOICE_LEAVES.get(m.group(1), "invoice.invoice_line_ids") if m else "invoice.invoice_line_ids"
    if rule in SCHEME_RULES:
        return f"{party or 'partner'}.peppol_eas"
    if party:
        if "PartyTaxScheme" in steps:
            return f"{party}.vat"
        if "PartyLegalEntity" in steps and leaf == "CompanyID":
            return f"{party}.company_registry"
        if leaf in LEAF_FIELDS and leaf != "Party":
            return f"{party}.{LEAF_FIELDS[leaf]}"
    if "PaymentMeans" in steps:
        return "bank.acc_number"
    if "InvoiceLine" in steps or "CreditNoteLine" in steps:
        if leaf in ("InvoicedQuantity", "CreditedQuantity"):
            return "line.product_uom_id"
        if leaf == "Name" and "Item" in steps:
            return "line.name"
        if "ClassifiedTaxCategory" in steps and leaf == "ID":
            return "tax.edi_tax_category"
        if leaf == "Percent":
            return "line.tax_ids"
    if rule in RULES:
        target = RULES[rule]
        if target.startswith("partner.") and party == "company":
            return "company." + target.split(".", 1)[1]
        return target
    if leaf in INVOICE_LEAVES and len(steps) == 2:
        return INVOICE_LEAVES[leaf]
    if party:
        return f"{party}.name"
    return "invoice.invoice_line_ids"


def label(target: str) -> str:
    labels = {
        "partner.peppol_endpoint": _("Customer: Peppol address"),
        "partner.peppol_eas": _("Customer: Peppol scheme"),
        "partner.vat": _("Customer: tax ID"),
        "partner.country_id": _("Customer: country"),
        "partner.name": _("Customer: name"),
        "partner.street": _("Customer: address"),
        "partner.city": _("Customer: city"),
        "partner.zip": _("Customer: postal code"),
        "partner.company_registry": _("Customer: registration number"),
        "company.peppol_endpoint": _("Company: Peppol address"),
        "company.peppol_eas": _("Company: Peppol scheme"),
        "company.vat": _("Company: tax ID"),
        "company.country_id": _("Company: country"),
        "company.name": _("Company: name"),
        "company.street": _("Company: address"),
        "company.company_registry": _("Company: registration number"),
        "invoice.ref": _("Invoice: customer reference"),
        "invoice.invoice_date": _("Invoice: date"),
        "invoice.invoice_date_due": _("Invoice: due date or payment terms"),
        "invoice.currency_id": _("Invoice: currency"),
        "invoice.move_type": _("Invoice: document type"),
        "invoice.invoice_line_ids": _("Invoice: lines and totals"),
        "invoice.name": _("Invoice: number"),
        "line.name": _("Invoice line: description"),
        "line.tax_ids": _("Invoice line: taxes"),
        "line.product_uom_id": _("Invoice line: unit of measure"),
        "line.price_unit": _("Invoice line: price"),
        "tax.edi_tax_category": _("Tax: e-invoice tax category"),
        "tax.edi_exemption_reason": _("Tax: exemption reason"),
        "bank.acc_number": _("Company: bank account for payments"),
    }
    return str(labels.get(target, target))


def fallback_fix(target: str) -> str:
    kind = target.split(".", 1)[0]
    texts = {
        "partner": _("Open the customer and complete this field, then generate the e-invoice again."),
        "company": _("Complete this field in the company settings, then generate the e-invoice again."),
        "invoice": _("Correct the invoice (reset it to draft if needed), then generate the e-invoice again."),
        "line": _("Correct the invoice line, then generate the e-invoice again."),
        "tax": _("Complete the e-invoice settings of the tax, then generate the e-invoice again."),
        "bank": _("Set the bank account on the company's bank journal, then generate the e-invoice again."),
    }
    return str(texts.get(kind, texts["invoice"]))


def _link(doc: Model, target: str, finding: dict[str, Any]) -> dict[str, Any] | None:
    """Where to fix it: (model, record id, field) of the invoice, its customer, its company, line, tax or bank."""
    rec = doc.elevated()
    move = rec.move_id
    kind, field = target.split(".", 1)
    if kind == "partner" and move:
        partner = move.commercial_partner_id or move.partner_id
        return {"model": "contacts.partner", "res_id": partner.ids[0], "field": field} if partner else None
    if kind == "company":
        return {"model": "core.company", "res_id": rec.company_id.ids[0], "field": field}
    if not move:
        return None
    lines = move.invoice_line_ids.filtered(lambda r: r.display_type == "product")
    index = next((i for name, i in _steps(str(finding.get("location") or "")) if name.endswith("Line")), 1)
    line = lines[index - 1 : index] if lines else lines
    if kind == "line" and line:
        return {"model": "account.move_line", "res_id": line.ids[0], "field": field, "move_id": move.ids[0]}
    if kind == "tax" and line and line.tax_ids:
        return {"model": "account.tax", "res_id": line.tax_ids.ids[0], "field": field}
    if kind == "tax" and line:  # no tax on the line: the line's taxes are what to fix
        return {"model": "account.move_line", "res_id": line.ids[0], "field": "tax_ids", "move_id": move.ids[0]}
    if kind == "bank":
        return {"model": "core.company", "res_id": rec.company_id.ids[0], "field": "bank_account"}
    return {"model": "account.move", "res_id": move.ids[0], "field": field}


def groups(doc: Model) -> list[dict[str, Any]]:
    """The document's findings grouped by rule (fatal first), each with its field and link."""
    out: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for f in doc.elevated().errors or []:
        rule = str(f.get("rule_id") or "")
        target = attribute(f)
        key = f"{rule}|{target}"
        if key not in out:
            out[key] = {
                "rule_id": rule,
                "flag": f.get("flag") or "fatal",
                "message": str(f.get("message") or "")[:500],
                "source": f.get("source"),
                "count": 0,
                "target": target,
                "label": label(target),
                "link": _link(doc, target, f),
            }
        out[key]["count"] += 1
    return sorted(out.values(), key=lambda g: (g["flag"] != "fatal", g["rule_id"]))


def _facts(doc: Model, items: list[dict[str, Any]]) -> dict[str, Any]:
    move = doc.elevated().move_id
    return {
        "findings": [{"rule": g["rule_id"], "message": g["message"], "count": g["count"]} for g in items],
        "invoice": {
            "number": move.name if move else None,
            "date": move.invoice_date.isoformat() if move and move.invoice_date else None,
            "untaxed": float(move.amount_untaxed or 0) if move else 0.0,
            "tax": float(move.amount_tax or 0) if move else 0.0,
            "total": float(move.amount_total or 0) if move else 0.0,
        },
    }


def _readable(env: Environment, doc: Model | None) -> Model:
    if doc is None or doc._name != "account.edi_document":
        raise UserError(_("Choose an e-invoice"))
    doc.check_access("read")
    return doc


@handler(FEATURE)
def prepare(env: Environment, record: Model | None, inputs: dict[str, Any]) -> dict[str, Any]:
    doc = _readable(env, record)
    items = groups(doc)
    rec = doc.elevated()
    move = rec.move_id
    f = _facts(doc, items)["invoice"]
    currency = move.currency_id.name if move and move.currency_id else ""
    lines = [
        f"E-invoice of {f['number'] or '-'} to {(move.partner_id.display_name if move else '') or '-'}",
        f"Invoice date {f['date'] or '-'}, untaxed {f['untaxed']:.2f}, tax {f['tax']:.2f}, total {f['total']:.2f} "
        f"{currency}",
        "Validation findings (rule, how many, where to fix, validator message):",
        *[f"- {g['rule_id']} x{g['count']} [{g['label']}]: {g['message']}" for g in items],
    ]
    return {"sources": [Source(f"account.edi_document:{doc.ids[0]}", "E-invoice findings", "\n".join(lines))]}


def explain(env: Environment, doc: Model) -> dict[str, Any]:
    """Every finding of the document with the field to fix (deterministic) and the model's explanation and hint
    (figures checked; the catalogue's wording when the model is unavailable or skips a rule)."""
    ensure_enabled(env, FEATURE)
    _readable(env, doc)
    started = time.monotonic()
    items = groups(doc)
    facts = _facts(doc, items)
    texts: dict[str, dict[str, str]] = {}
    removed: list[str] = []
    flags: list[str] = []
    run = None
    if items:
        try:
            result = run_feature(env, FEATURE, {}, record=doc, **HANDLERS[FEATURE](env, doc, {}))
            run = result.run
            data = result.data if isinstance(result.data, dict) else {}
            known = {g["rule_id"] for g in items}
            for row in data.get("items") or []:
                rule = str(row.get("rule_id") or "")
                if rule not in known:
                    flags.append("unknown_rule_ignored")
                    continue
                explanation, gone = ground_text(str(row.get("explanation") or ""), facts)
                fix, gone_fix = ground_text(str(row.get("fix") or ""), facts)
                removed += gone + gone_fix
                texts[rule] = {"explanation": explanation, "fix": fix}
        except AIError as exc:
            flags.append(exc.code)
    for g in items:
        text = texts.get(g["rule_id"]) or {}
        g["explanation"] = text.get("explanation") or g["message"]
        g["fix"] = text.get("fix") or fallback_fix(g["target"])
        g["explained_by"] = "model" if text.get("explanation") else "catalogue"
    if run is None:
        run = audit_run(env, FEATURE, record=doc, output=[g["target"] for g in items], model="rule catalogue",
                        started=started)  # fmt: skip
    return {
        "run_id": run.ids[0],
        "document_id": doc.ids[0],
        "items": items,
        "removed_numbers": removed,
        "flags": flags,
        "ai_generated": True,
    }


# ---------------------------------------------------------------------------------------------- API

router = register_router(APIRouter(prefix="/account_einvoice/ai", tags=["account_einvoice", "ai"]))


@router.post("/documents/{doc_id}/explain")
def explain_document(doc_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    """Explanations and fix hints for the validation findings of an e-invoice (nothing is written)."""
    ensure_enabled(ctx.env, FEATURE)
    doc = ctx.env["account.edi_document"].browse([doc_id])
    if not doc.exists():
        raise MissingError(_("No such e-invoice"))
    return explain(ctx.env, doc)


# ---------------------------------------------------------------------------------------------- evaluation


def eval_entry(env: Environment, inputs: dict[str, Any], record: Any) -> dict[str, Any]:
    out = explain(env, record)
    text = "\n".join(f"{g['rule_id']} ({g['label']}): {g['explanation']} {g['fix']}" for g in out["items"])
    data = {
        "targets": {g["rule_id"]: g["target"] for g in out["items"]},
        "links": [g["link"] for g in out["items"]],
        "by_model": sum(1 for g in out["items"] if g["explained_by"] == "model"),
        "removed_numbers": out["removed_numbers"],
        "flags": out["flags"],
    }
    return {"text": text, "data": data, "proposals": 0, "status": "ok", "flags": out["flags"]}


def score(results: list[dict[str, Any]]) -> dict[str, float]:
    """``field_attribution``: labelled findings attributed to the field a person must fix (≥ 0.9);
    ``link_coverage``: findings with a link target; ``explained``: findings explained by the model;
    ``guard_accuracy``: probe cases whose invented figures were removed exactly."""
    ok = [r for r in results if r.get("status") == "ok" and isinstance(r.get("data"), dict)]
    if not ok:
        return {}
    right = total = 0
    for r in ok:
        for rule, want in (r["_case"]["expected"].get("targets") or {}).items():
            total += 1
            right += 1 if r["data"]["targets"].get(rule) == want else 0
    links = [link for r in ok for link in r["data"]["links"]]
    groups_total = sum(len(r["data"]["targets"]) for r in ok)
    out = {
        "field_attribution": right / total if total else 1.0,
        "link_coverage": sum(1 for link in links if link) / len(links) if links else 1.0,
        "explained": sum(r["data"]["by_model"] for r in ok) / groups_total if groups_total else 1.0,
    }
    probes = [r for r in ok if "removed" in r["_case"]["expected"]]
    if probes:
        out["guard_accuracy"] = sum(
            1 for r in probes if sorted(r["data"]["removed_numbers"]) == sorted(r["_case"]["expected"]["removed"])
        ) / len(probes)
    return out
