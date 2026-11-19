"""UBL 2.1 Invoice / CreditNote following Peppol BIS Billing 3.0 (EN 16931 core invoice usage).

Built from a posted customer invoice or credit note. Elements follow the UBL schema order. Tax categories
(UNCL5305: S, Z, E, AE, K, G, O, L, M) come from the taxes (``edi_tax_category``, else derived from the rate
and name); exempt, reverse-charge, intra-community and export categories carry a VATEX reason. A line
discount is a price allowance (gross price, allowance per unit, net price), so line amounts equal the
invoice's lines exactly. Totals are recomputed from the document so the BR-CO sums hold.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from lxml import etree

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model

CUSTOMIZATION_ID = "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
PROFILE_ID = "urn:fdc:peppol.eu:2017:poacc:billing:01:1.0"
NS = {
    "Invoice": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
    "CreditNote": "urn:oasis:names:specification:ubl:schema:xsd:CreditNote-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
}
VAT_EAS = {"FR": "9957", "BE": "9925", "DE": "9930", "NL": "9944", "AT": "9914", "ES": "9920", "IT": "0211"}
EXEMPTIONS = {
    "E": ("VATEX-EU-132", "Exempt from VAT"),
    "AE": ("VATEX-EU-AE", "Reverse charge"),
    "K": ("VATEX-EU-IC", "Intra-community supply"),
    "G": ("VATEX-EU-G", "Export outside the EU"),
    "O": ("VATEX-EU-O", "Not subject to VAT"),
}
UNIT_CODES = {
    "unit": "C62", "units": "C62", "unit(s)": "C62", "piece": "C62", "pcs": "C62", "each": "C62",
    "hour": "HUR", "hours": "HUR", "hour(s)": "HUR", "h": "HUR", "day": "DAY", "days": "DAY", "day(s)": "DAY",
    "kg": "KGM", "g": "GRM", "t": "TNE", "m": "MTR", "km": "KMT", "cm": "CMT", "l": "LTR", "litre": "LTR",
    "liter": "LTR", "m2": "MTK", "m³": "MTQ", "month": "MON", "months": "MON", "year": "ANN", "week": "WEE",
}  # fmt: skip


def _q(value: float | Decimal, places: int = 2) -> Decimal:
    return Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _amount(value: float | Decimal, places: int = 2) -> str:
    return f"{_q(value, places):.{places}f}"


def _num(value: float | Decimal) -> str:
    text = f"{Decimal(str(value)).normalize():f}"
    return text if text not in ("-0",) else "0"


class Builder:
    def __init__(self, root: etree._Element) -> None:
        self.root = root

    @staticmethod
    def sub(parent: etree._Element, tag: str, text: Any = None, **attrs: str) -> etree._Element:
        prefix, _sep, local = tag.partition(":")
        el = etree.SubElement(parent, f"{{{NS[prefix]}}}{local}", {k: v for k, v in attrs.items() if v is not None})
        if text is not None:
            el.text = str(text)
        return el


@dataclass
class TaxInfo:
    category: str
    percent: Decimal
    reason_code: str | None
    reason: str | None

    @property
    def key(self) -> tuple[str, Decimal]:
        return (self.category, self.percent)


def tax_category(taxes: Model) -> TaxInfo:
    """The UBL tax category of a line's taxes (one VAT category per line; groups add up)."""
    flat = [t for tax in taxes for t in tax._flatten()] if taxes else []
    percent = _q(sum(float(t.amount or 0) for t in flat if t.amount_type in ("percent", "division")), 2)
    explicit = next((t for t in flat if t.edi_tax_category), None)
    if explicit is not None:
        category = explicit.edi_tax_category
        code = explicit.edi_exemption_reason_code
        reason = explicit.edi_exemption_reason
    else:
        names = " ".join(f"{t.name or ''} {t.description or ''}" for t in flat).lower()
        if percent > 0:
            category = "S"
        elif "reverse" in names or "autoliquidation" in names:
            category = "AE"
        elif "intra" in names:
            category = "K"
        elif "export" in names:
            category = "G"
        elif "exempt" in names or not flat:
            category = "E"
        else:
            category = "Z"
        code = reason = None
    if category in EXEMPTIONS:
        default_code, default_reason = EXEMPTIONS[category]
        code = code or default_code
        reason = reason or default_reason
    else:
        code = reason = None
    if category in ("Z", "E", "AE", "K", "G", "O"):
        percent = Decimal("0.00") if category != "Z" or percent == 0 else percent
    return TaxInfo(category=category, percent=percent, reason_code=code, reason=reason)


def endpoint_of(record: Model) -> tuple[str, str] | None:
    """(scheme, id) of a partner's or company's Peppol address; derived from the VAT number when possible."""
    if record.peppol_eas and record.peppol_endpoint:
        return record.peppol_eas, record.peppol_endpoint.strip()
    vat = (record.vat or "").replace(" ", "").upper()
    country = record.country_id.code if record.country_id else (vat[:2] if vat[:2].isalpha() else None)
    if vat and country in VAT_EAS:
        return VAT_EAS[country], vat
    return None


def _unit_code(line: Model) -> str:
    name = (line.uom_id.name or "").strip().lower() if line.uom_id else ""
    return UNIT_CODES.get(name, "C62")


class UblBuilder:
    def __init__(self, move: Model) -> None:
        self.move = move.elevated("e-invoice")
        if self.move.state != "posted" or self.move.move_type not in ("out_invoice", "out_refund"):
            raise UserError(_("Only posted customer invoices and credit notes can be sent as e-invoices"))
        self.kind = "Invoice" if self.move.move_type == "out_invoice" else "CreditNote"
        self.currency = self.move.currency_id
        self.company = self.move.company_id
        self.partner = self.move.commercial_partner_id or self.move.partner_id
        self.places = int(self.currency.decimal_places or 2)

    # -- helpers -----------------------------------------------------------------------------------

    def amt(
        self, parent: etree._Element, tag: str, value: float | Decimal, currency: str | None = None
    ) -> etree._Element:
        return Builder.sub(parent, tag, _amount(value, self.places), currencyID=currency or self.currency.name)

    def _address(self, parent: etree._Element, rec: Model, tag: str = "cac:PostalAddress") -> None:
        address = Builder.sub(parent, tag)
        if rec.street:
            Builder.sub(address, "cbc:StreetName", rec.street)
        if rec.street2:
            Builder.sub(address, "cbc:AdditionalStreetName", rec.street2)
        if rec.city:
            Builder.sub(address, "cbc:CityName", rec.city)
        if rec.zip:
            Builder.sub(address, "cbc:PostalZone", rec.zip)
        country = Builder.sub(address, "cac:Country")
        Builder.sub(country, "cbc:IdentificationCode", rec.country_id.code if rec.country_id else "")

    def _party(self, parent: etree._Element, rec: Model, *, seller: bool) -> None:
        party = Builder.sub(parent, "cac:Party")
        endpoint = endpoint_of(rec)
        if endpoint is None:
            who = _("the company") if seller else rec.name
            raise UserError(_("Set the Peppol address (scheme and identifier) of {who}", who=who))
        Builder.sub(party, "cbc:EndpointID", endpoint[1], schemeID=endpoint[0])
        name = Builder.sub(party, "cac:PartyName")
        Builder.sub(name, "cbc:Name", rec.name)
        self._address(party, rec)
        if rec.vat:
            scheme = Builder.sub(party, "cac:PartyTaxScheme")
            Builder.sub(scheme, "cbc:CompanyID", rec.vat.replace(" ", ""))
            Builder.sub(Builder.sub(scheme, "cac:TaxScheme"), "cbc:ID", "VAT")
        legal = Builder.sub(party, "cac:PartyLegalEntity")
        Builder.sub(legal, "cbc:RegistrationName", rec.name)
        registry = getattr(rec, "company_registry", None) if seller else None
        if registry:
            Builder.sub(legal, "cbc:CompanyID", registry)
        if rec.email or rec.phone:
            contact = Builder.sub(party, "cac:Contact")
            if rec.phone:
                Builder.sub(contact, "cbc:Telephone", rec.phone)
            if rec.email:
                Builder.sub(contact, "cbc:ElectronicMail", rec.email)

    def _iban(self) -> tuple[str, str | None] | None:
        journals = (
            self.move.env["account.journal"]
            .elevated()
            .search(
                [("company_id", "=", self.company.ids[0]), ("type", "=", "bank"), ("bank_account_id", "is_set")],
                order="sequence, id",
            )
        )
        for journal in journals:
            bank = journal.bank_account_id
            if bank.currency_id and bank.currency_id.ids != self.currency.ids:
                continue
            return bank.sanitized_acc_number, bank.bic
        return None

    def _lines(self) -> list[dict[str, Any]]:
        out = []
        for line in self.move.invoice_line_ids.filtered(lambda r: r.display_type == "product").sorted(
            lambda r: (r.sequence or 0, r.ids[0])
        ):
            taxes = line.tax_ids
            quantity = Decimal(str(line.quantity or 0))
            gross = Decimal(str(line.price_unit or 0))
            discount = Decimal(str(line.discount or 0))
            subtotal = _q(line.price_subtotal or 0, self.places)
            if quantity:
                net = (subtotal / quantity).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
                if net * quantity != subtotal:  # price-included taxes or rounding: state the exact net price
                    net = subtotal / quantity
                    net = net.quantize(Decimal("0.0000000001"), rounding=ROUND_HALF_UP)
            else:
                net = gross * (1 - discount / 100)
            if net < 0:  # BR-27 / BR-28: prices are never negative, a deduction has a negative quantity
                net, gross, quantity = -net, -gross, -quantity
            out.append(
                {
                    "line": line,
                    "quantity": quantity,
                    "gross": gross if discount else None,
                    "net": net,
                    "subtotal": subtotal,
                    "tax": tax_category(taxes),
                }
            )
        if not out:
            raise UserError(_("The invoice has no lines"))
        return out

    def _tax_amounts(self, lines: list[dict[str, Any]]) -> OrderedDict[tuple[str, Decimal], dict[str, Any]]:
        groups: OrderedDict[tuple[str, Decimal], dict[str, Any]] = OrderedDict()
        for info in lines:
            slot = groups.setdefault(
                info["tax"].key, {"tax": info["tax"], "base": Decimal(0), "amount": Decimal(0), "taxes": set()}
            )
            slot["base"] += info["subtotal"]
            slot["taxes"].update(t.ids[0] for tax in info["line"].tax_ids for t in tax._flatten())
        tax_lines = self.move.line_ids.filtered(lambda r: r.display_type == "tax")
        sign = -1 if self.move.move_type == "out_invoice" else 1
        assigned = Decimal(0)
        for tl in tax_lines:
            value = _q(sign * float(tl.amount_currency), self.places)
            for slot in groups.values():
                if tl.tax_line_id.ids and tl.tax_line_id.ids[0] in slot["taxes"]:
                    slot["amount"] += value
                    assigned += value
                    break
        return groups

    # -- document ----------------------------------------------------------------------------------

    def build(self) -> bytes:
        move = self.move
        kind = self.kind
        root = etree.Element(f"{{{NS[kind]}}}{kind}", nsmap={None: NS[kind], "cac": NS["cac"], "cbc": NS["cbc"]})
        b = Builder.sub
        b(root, "cbc:CustomizationID", CUSTOMIZATION_ID)
        b(root, "cbc:ProfileID", PROFILE_ID)
        b(root, "cbc:ID", move.name)
        b(root, "cbc:IssueDate", (move.invoice_date or move.date).isoformat())
        if kind == "Invoice" and move.invoice_date_due:
            b(root, "cbc:DueDate", move.invoice_date_due.isoformat())
        b(
            root,
            "cbc:InvoiceTypeCode" if kind == "Invoice" else "cbc:CreditNoteTypeCode",
            "380" if kind == "Invoice" else "381",
        )
        if move.narration:
            text = re.sub(r"<[^>]+>", " ", move.narration)
            text = " ".join(text.split())
            if text:
                b(root, "cbc:Note", text[:1000])
        b(root, "cbc:DocumentCurrencyCode", self.currency.name)
        ccur = self.company.currency_id
        foreign = ccur.ids != self.currency.ids
        if foreign:
            b(root, "cbc:TaxCurrencyCode", ccur.name)
        b(root, "cbc:BuyerReference", (move.ref or move.invoice_origin or self.partner.ref or move.name)[:200])
        if move.invoice_origin:
            b(b(root, "cac:OrderReference"), "cbc:ID", move.invoice_origin[:200])
        origin = move.reversed_entry_id
        if origin:
            ref = b(b(root, "cac:BillingReference"), "cac:InvoiceDocumentReference")
            b(ref, "cbc:ID", origin.name)
            if origin.invoice_date:
                b(ref, "cbc:IssueDate", origin.invoice_date.isoformat())
        self._party(b(root, "cac:AccountingSupplierParty"), self.company, seller=True)
        self._party(b(root, "cac:AccountingCustomerParty"), self.partner, seller=False)
        lines = self._lines()
        groups = self._tax_amounts(lines)
        if any(key[0] == "K" for key in groups):
            delivery = b(root, "cac:Delivery")
            b(delivery, "cbc:ActualDeliveryDate", (move.invoice_date or move.date).isoformat())
            location = b(delivery, "cac:DeliveryLocation")
            address = b(location, "cac:Address")
            b(
                b(address, "cac:Country"),
                "cbc:IdentificationCode",
                self.partner.country_id.code if self.partner.country_id else "",
            )
        iban = self._iban()
        if iban:
            means = b(root, "cac:PaymentMeans")
            b(means, "cbc:PaymentMeansCode", "58" if self.currency.name == "EUR" else "30", name="Credit transfer")
            if kind == "CreditNote" and move.invoice_date_due:
                b(means, "cbc:PaymentDueDate", move.invoice_date_due.isoformat())
            b(means, "cbc:PaymentID", (move.payment_reference or move.name)[:140])
            account = b(means, "cac:PayeeFinancialAccount")
            b(account, "cbc:ID", iban[0])
            b(account, "cbc:Name", self.company.name)
            if iban[1]:
                b(b(account, "cac:FinancialInstitutionBranch"), "cbc:ID", iban[1])
        # BR-CO-25: a positive amount due needs a due date or payment terms (credit notes have no due date)
        term = move.payment_term_id.elevated() if move.payment_term_id else None
        note = (term.note or term.name) if term else None
        note = " ".join(re.sub(r"<[^>]+>", " ", note).split()) if note else None
        if not note and move.invoice_date_due:
            note = str(_("Payable by {date}", date=move.invoice_date_due.isoformat()))
        if note:
            b(b(root, "cac:PaymentTerms"), "cbc:Note", note[:500])
        total_tax = sum((slot["amount"] for slot in groups.values()), Decimal(0))
        tax_total = b(root, "cac:TaxTotal")
        self.amt(tax_total, "cbc:TaxAmount", total_tax)
        for slot in groups.values():
            info: TaxInfo = slot["tax"]
            sub = b(tax_total, "cac:TaxSubtotal")
            self.amt(sub, "cbc:TaxableAmount", slot["base"])
            self.amt(sub, "cbc:TaxAmount", slot["amount"])
            self._category(sub, "cac:TaxCategory", info, with_reason=True)
        if foreign:
            sign = -1 if move.move_type == "out_invoice" else 1
            company_tax = sum(
                (
                    Decimal(str(sign * float(r.balance)))
                    for r in move.line_ids.filtered(lambda r: r.display_type == "tax")
                ),
                Decimal(0),
            )
            accounting = b(root, "cac:TaxTotal")
            Builder.sub(
                accounting, "cbc:TaxAmount", _amount(company_tax, int(ccur.decimal_places or 2)), currencyID=ccur.name
            )
        line_total = sum((info["subtotal"] for info in lines), Decimal(0))
        inclusive = line_total + total_tax
        paid = max(_q(float(move.amount_total) - float(move.amount_residual), self.places), Decimal(0))
        totals = b(root, "cac:LegalMonetaryTotal")
        self.amt(totals, "cbc:LineExtensionAmount", line_total)
        self.amt(totals, "cbc:TaxExclusiveAmount", line_total)
        self.amt(totals, "cbc:TaxInclusiveAmount", inclusive)
        if paid:
            self.amt(totals, "cbc:PrepaidAmount", paid)
        self.amt(totals, "cbc:PayableAmount", inclusive - paid)
        for i, info in enumerate(lines, start=1):
            self._line(root, i, info)
        return etree.tostring(root, xml_declaration=True, encoding="UTF-8", pretty_print=True)

    def _category(self, parent: etree._Element, tag: str, info: TaxInfo, *, with_reason: bool) -> None:
        cat = Builder.sub(parent, tag)
        Builder.sub(cat, "cbc:ID", info.category)
        if info.category != "O":
            Builder.sub(cat, "cbc:Percent", _num(info.percent))
        if with_reason and info.reason_code:
            Builder.sub(cat, "cbc:TaxExemptionReasonCode", info.reason_code)
        if with_reason and info.reason:
            Builder.sub(cat, "cbc:TaxExemptionReason", info.reason)
        Builder.sub(Builder.sub(cat, "cac:TaxScheme"), "cbc:ID", "VAT")

    def _line(self, root: etree._Element, index: int, info: dict[str, Any]) -> None:
        b = Builder.sub
        line = info["line"]
        tag = "cac:InvoiceLine" if self.kind == "Invoice" else "cac:CreditNoteLine"
        el = b(root, tag)
        b(el, "cbc:ID", str(index))
        qty_tag = "cbc:InvoicedQuantity" if self.kind == "Invoice" else "cbc:CreditedQuantity"
        b(el, qty_tag, _num(info["quantity"]), unitCode=_unit_code(line))
        self.amt(el, "cbc:LineExtensionAmount", info["subtotal"])
        item = b(el, "cac:Item")
        product = line.product_id.elevated() if line.product_id else None
        description = (line.name or "").strip()
        name = (product.name if product else None) or description.split("\n")[0] or _("Item")
        if description and description != name:
            b(item, "cbc:Description", description[:1000])
        b(item, "cbc:Name", str(name)[:200])
        code = getattr(product, "default_code", None) if product else None
        if code:
            b(b(item, "cac:SellersItemIdentification"), "cbc:ID", code)
        self._category(item, "cac:ClassifiedTaxCategory", info["tax"], with_reason=False)
        price = b(el, "cac:Price")
        b(price, "cbc:PriceAmount", _num(info["net"]), currencyID=self.currency.name)
        if info["gross"] is not None and info["gross"] > info["net"]:
            allowance = b(price, "cac:AllowanceCharge")
            b(allowance, "cbc:ChargeIndicator", "false")
            b(allowance, "cbc:Amount", _num(info["gross"] - info["net"]), currencyID=self.currency.name)
            b(allowance, "cbc:BaseAmount", _num(info["gross"]), currencyID=self.currency.name)


def build_ubl(move: Model) -> bytes:
    """The UBL 2.1 / Peppol BIS 3 XML of a posted customer invoice or credit note."""
    return UblBuilder(move).build()
