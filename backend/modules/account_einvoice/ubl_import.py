"""Inbound UBL invoices and credit notes → draft vendor bills / refunds.

The supplier is found by its Peppol address, then its VAT number, then its exact name (created when
unknown). Lines keep their description, quantity and net price; taxes are the company's purchase taxes of the
same rate (reverse-charge categories prefer a reverse-charge tax). Document-level allowances and charges
become lines. The received XML is attached to the bill.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from lxml import etree

from modules.account_einvoice.ubl import NS
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model

CAC, CBC = NS["cac"], NS["cbc"]


def _x(el: etree._Element | None, path: str) -> etree._Element | None:
    if el is None:
        return None
    found = el.find(path.replace("cac:", f"{{{CAC}}}").replace("cbc:", f"{{{CBC}}}"))
    return found


def _t(el: etree._Element | None, path: str) -> str | None:
    node = _x(el, path)
    return node.text.strip() if node is not None and node.text else None


def _all(el: etree._Element, path: str) -> list[etree._Element]:
    return el.findall(path.replace("cac:", f"{{{CAC}}}").replace("cbc:", f"{{{CBC}}}"))


def parse(xml: bytes) -> dict[str, Any]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        root = etree.fromstring(xml, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise UserError(_("The e-invoice is not valid XML: {error}", error=str(exc))) from exc
    kind = etree.QName(root).localname
    if kind not in ("Invoice", "CreditNote") or etree.QName(root).namespace != NS[kind]:
        raise UserError(_("Not a UBL invoice or credit note"))
    supplier = _x(root, "cac:AccountingSupplierParty/cac:Party")
    endpoint = _x(supplier, "cbc:EndpointID")
    line_tag = "cac:InvoiceLine" if kind == "Invoice" else "cac:CreditNoteLine"
    qty_tag = "cbc:InvoicedQuantity" if kind == "Invoice" else "cbc:CreditedQuantity"
    lines = []
    for line in _all(root, line_tag):
        quantity = Decimal(_t(line, qty_tag) or "1")
        price = Decimal(_t(line, "cac:Price/cbc:PriceAmount") or "0")
        base_qty = Decimal(_t(line, "cac:Price/cbc:BaseQuantity") or "1") or Decimal(1)
        lines.append(
            {
                "name": _t(line, "cac:Item/cbc:Name") or _t(line, "cac:Item/cbc:Description") or "/",
                "description": _t(line, "cac:Item/cbc:Description"),
                "quantity": float(quantity),
                "price_unit": float(price / base_qty),
                "amount": float(Decimal(_t(line, "cbc:LineExtensionAmount") or "0")),
                "category": _t(line, "cac:Item/cac:ClassifiedTaxCategory/cbc:ID"),
                "percent": float(_t(line, "cac:Item/cac:ClassifiedTaxCategory/cbc:Percent") or 0),
            }
        )
    for charge in _all(root, "cac:AllowanceCharge"):
        is_charge = (_t(charge, "cbc:ChargeIndicator") or "false").lower() == "true"
        amount = float(Decimal(_t(charge, "cbc:Amount") or "0"))
        lines.append(
            {
                "name": _t(charge, "cbc:AllowanceChargeReason") or (_("Charge") if is_charge else _("Allowance")),
                "description": None,
                "quantity": 1.0,
                "price_unit": amount if is_charge else -amount,
                "amount": amount if is_charge else -amount,
                "category": _t(charge, "cac:TaxCategory/cbc:ID"),
                "percent": float(_t(charge, "cac:TaxCategory/cbc:Percent") or 0),
            }
        )
    return {
        "kind": kind,
        "number": _t(root, "cbc:ID"),
        "issue_date": _t(root, "cbc:IssueDate"),
        "due_date": _t(root, "cbc:DueDate") or _t(root, "cac:PaymentMeans/cbc:PaymentDueDate"),
        "currency": _t(root, "cbc:DocumentCurrencyCode"),
        "supplier": {
            "name": _t(supplier, "cac:PartyLegalEntity/cbc:RegistrationName") or _t(supplier, "cac:PartyName/cbc:Name"),
            "vat": _t(supplier, "cac:PartyTaxScheme/cbc:CompanyID"),
            "eas": endpoint.get("schemeID") if endpoint is not None else None,
            "endpoint": endpoint.text.strip() if endpoint is not None and endpoint.text else None,
            "country": _t(supplier, "cac:PostalAddress/cac:Country/cbc:IdentificationCode"),
            "email": _t(supplier, "cac:Contact/cbc:ElectronicMail"),
        },
        "payable": float(Decimal(_t(root, "cac:LegalMonetaryTotal/cbc:PayableAmount") or "0")),
        "tax_total": float(Decimal(_t(root, "cac:TaxTotal/cbc:TaxAmount") or "0")),
        "lines": lines,
    }


def _supplier(env: Any, company: Model, data: dict[str, Any]) -> Model:
    partners = env["contacts.partner"].elevated()
    info = data["supplier"]
    usable: list[Any] = [{"any": [("company_id", "is_not_set"), ("company_id", "=", company.ids[0])]}]
    if info["eas"] and info["endpoint"]:
        found = partners.search(
            [("peppol_eas", "=", info["eas"]), ("peppol_endpoint", "=", info["endpoint"]), *usable], limit=1
        )
        if found:
            return found
    if info["vat"]:
        found = partners.search([("vat", "=", info["vat"]), ("parent_id", "is_not_set"), *usable], limit=1)
        if found:
            return found
    if info["name"]:
        found = partners.search([("name", "=", info["name"]), ("is_company", "=", True), *usable], limit=1)
        if found:
            return found
    country = (
        env["core.country"].elevated().search([("code", "=", info["country"])], limit=1) if info["country"] else None
    )
    return partners.create(
        {
            "name": info["name"] or info["endpoint"] or _("Unknown supplier"),
            "is_company": True,
            "vat": info["vat"],
            "email": info["email"],
            "peppol_eas": info["eas"],
            "peppol_endpoint": info["endpoint"],
            "country_id": country.ids[0] if country else None,
        }
    )


def _purchase_tax(env: Any, company: Model, category: str | None, percent: float) -> list[int]:
    taxes = (
        env["account.tax"]
        .elevated()
        .search(
            [("company_id", "=", company.ids[0]), ("type_tax_use", "=", "purchase"), ("amount_type", "=", "percent")],
            order="sequence, id",
        )
    )
    same = [t for t in taxes if abs(float(t.amount or 0) - percent) < 1e-6 and not t.price_include]
    if category in ("AE", "K"):
        reverse = [t for t in taxes if t.edi_tax_category == "AE" or "reverse" in (t.name or "").lower()]
        if reverse:
            return reverse[0].ids
    if category in ("Z", "E", "G", "O") or not percent:
        zero = [t for t in same if (t.edi_tax_category or "") == (category or "")] or same
        return zero[0].ids if zero else []
    return same[0].ids if same else []


def create_bill(company: Model, xml: bytes, *, filename: str = "e-invoice.xml") -> Model:
    """A draft vendor bill (or refund) in ``company`` from a received UBL document."""
    env = company.env
    data = parse(xml)
    supplier = _supplier(env, company, data)
    currency = (
        env["core.currency"]
        .elevated()
        .with_context(active_test=False)
        .search([("name", "=", data["currency"])], limit=1)
    )
    move_type = "in_invoice" if data["kind"] == "Invoice" else "in_refund"
    issue = dt.date.fromisoformat(data["issue_date"]) if data["issue_date"] else env.today()
    vals: dict[str, Any] = {
        "move_type": move_type,
        "partner_id": supplier.ids[0],
        "company_id": company.ids[0],
        "invoice_date": issue,
        "ref": data["number"],
        "invoice_line_ids": [
            (
                "create",
                {
                    "name": line["description"] or line["name"],
                    "quantity": line["quantity"],
                    "price_unit": line["price_unit"],
                    "tax_ids": [("set", _purchase_tax(env, company, line["category"], line["percent"]))],
                },
            )
            for line in data["lines"]
        ],
    }
    if currency:
        vals["currency_id"] = currency.ids[0]
    move = env["account.move"].elevated("e-invoice import").with_company(company).create(vals)
    if data["due_date"]:
        move.with_context(_skip_sync=True).write({"invoice_date_due": dt.date.fromisoformat(data["due_date"])})
    env["core.attachment"].elevated().create(
        {
            "name": filename,
            "mimetype": "application/xml",
            "model": "account.move",
            "res_id": move.ids[0],
            "raw": xml,
            "company_id": company.ids[0],
        }
    )
    return move
