# ruff: noqa: E501 - the defect table is kept one defect per line
"""Defects applied to a valid Peppol BIS 3 invoice (``fixtures/base_invoice.xml``) to obtain the findings the vendored
validators really emit, for the ``account_einvoice.error_explain`` evaluation (``build_evals.py``).

Each mutation names the field a person must fix (``target``: ``partner.<field>``, ``company.<field>``,
``invoice.<field>``, ``line.<field>``, ``tax.<field>`` or ``bank.<field>``) — the label the feature's attribution is
scored against.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from lxml import etree

HERE = Path(__file__).parent
NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "inv": "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2",
}
BUYER = "/inv:Invoice/cac:AccountingCustomerParty/cac:Party"
SELLER = "/inv:Invoice/cac:AccountingSupplierParty/cac:Party"
LINE1 = "/inv:Invoice/cac:InvoiceLine[1]"


def base() -> etree._Element:
    return etree.fromstring((HERE / "fixtures" / "base_invoice.xml").read_bytes())


def _one(root: etree._Element, path: str) -> etree._Element:
    return root.xpath(path, namespaces=NS)[0]


def remove(path: str) -> Callable[[etree._Element], None]:
    def fn(root: etree._Element) -> None:
        for el in root.xpath(path, namespaces=NS):
            el.getparent().remove(el)

    return fn


def text(path: str, value: str) -> Callable[[etree._Element], None]:
    def fn(root: etree._Element) -> None:
        for el in root.xpath(path, namespaces=NS):
            el.text = value

    return fn


def attr(path: str, name: str, value: str) -> Callable[[etree._Element], None]:
    def fn(root: etree._Element) -> None:
        for el in root.xpath(path, namespaces=NS):
            el.set(name, value)

    return fn


def both(*fns: Callable[[etree._Element], None]) -> Callable[[etree._Element], None]:
    def fn(root: etree._Element) -> None:
        for f in fns:
            f(root)

    return fn


def exempt_without_reason(root: etree._Element) -> None:
    """Line 1 and its subtotal become exempt (category E, 0 %) without an exemption reason."""
    for path in (
        f"{LINE1}/cac:Item/cac:ClassifiedTaxCategory",
        "/inv:Invoice/cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory",
    ):
        for cat in root.xpath(path, namespaces=NS):
            _one(cat, "cbc:ID").text = "E"
            _one(cat, "cbc:Percent").text = "0"


# (name, target, mutation, description of the defect for the reference text)
# fmt: off
MUTATIONS: list[tuple[str, str, Callable[[etree._Element], None], str]] = [
    ("buyer_endpoint_missing", "partner.peppol_endpoint", remove(f"{BUYER}/cbc:EndpointID"), "the customer has no Peppol address"),
    ("seller_endpoint_missing", "company.peppol_endpoint", remove(f"{SELLER}/cbc:EndpointID"), "the company has no Peppol address"),
    ("buyer_scheme_invalid", "partner.peppol_eas", attr(f"{BUYER}/cbc:EndpointID", "schemeID", "9999"), "the customer's Peppol scheme is not a valid EAS code"),
    ("seller_scheme_invalid", "company.peppol_eas", attr(f"{SELLER}/cbc:EndpointID", "schemeID", "1234"), "the company's Peppol scheme is not a valid EAS code"),
    ("buyer_reference_missing", "invoice.ref", remove("/inv:Invoice/cbc:BuyerReference"), "the invoice has neither a customer reference nor an order reference"),
    ("seller_vat_missing", "company.vat", remove(f"{SELLER}/cac:PartyTaxScheme"), "the company has no VAT number although it charges VAT"),
    ("seller_vat_prefix", "company.vat", text(f"{SELLER}/cac:PartyTaxScheme/cbc:CompanyID", "334175221"), "the company's VAT number has no country prefix"),
    ("buyer_vat_prefix", "partner.vat", text(f"{BUYER}/cac:PartyTaxScheme/cbc:CompanyID", "40303265045"), "the customer's VAT number has no country prefix"),
    ("buyer_country_missing", "partner.country_id", text(f"{BUYER}/cac:PostalAddress/cac:Country/cbc:IdentificationCode", ""), "the customer has no country"),
    ("seller_country_invalid", "company.country_id", text(f"{SELLER}/cac:PostalAddress/cac:Country/cbc:IdentificationCode", "XX"), "the company's country code is not valid"),
    ("buyer_country_invalid", "partner.country_id", text(f"{BUYER}/cac:PostalAddress/cac:Country/cbc:IdentificationCode", "ZZ"), "the customer's country code is not valid"),
    ("buyer_name_missing", "partner.name", text(f"{BUYER}/cac:PartyLegalEntity/cbc:RegistrationName", ""), "the customer's legal name is empty"),
    ("seller_name_missing", "company.name", text(f"{SELLER}/cac:PartyLegalEntity/cbc:RegistrationName", ""), "the company's legal name is empty"),
    ("currency_invalid", "invoice.currency_id", both(text("/inv:Invoice/cbc:DocumentCurrencyCode", "EURO"), attr("//*[@currencyID]", "currencyID", "EURO")), "the invoice currency is not an ISO 4217 code"),
    ("payable_wrong", "invoice.invoice_line_ids", text("/inv:Invoice/cac:LegalMonetaryTotal/cbc:PayableAmount", "999.00"), "the amount due does not match the invoice total"),
    ("tax_total_wrong", "invoice.invoice_line_ids", text("/inv:Invoice/cac:TaxTotal/cbc:TaxAmount", "150.00"), "the tax total does not match the tax subtotals"),
    ("line_total_wrong", "invoice.invoice_line_ids", text("/inv:Invoice/cac:LegalMonetaryTotal/cbc:LineExtensionAmount", "700.00"), "the sum of the lines does not match the lines"),
    ("inclusive_wrong", "invoice.invoice_line_ids", text("/inv:Invoice/cac:LegalMonetaryTotal/cbc:TaxInclusiveAmount", "880.00"), "the total with tax is not the total without tax plus the tax"),
    ("due_date_and_terms_missing", "invoice.invoice_date_due", both(remove("/inv:Invoice/cbc:DueDate"), remove("/inv:Invoice/cac:PaymentTerms"), remove("/inv:Invoice/cac:PaymentMeans/cbc:PaymentDueDate")), "the invoice has an amount due but neither a due date nor payment terms"),
    ("iban_missing", "bank.acc_number", remove("/inv:Invoice/cac:PaymentMeans/cac:PayeeFinancialAccount"), "the payment means is a credit transfer without the company's bank account"),
    ("line_name_missing", "line.name", text(f"{LINE1}/cac:Item/cbc:Name", ""), "a line has no description"),
    ("line_percent_missing", "line.tax_ids", remove(f"{LINE1}/cac:Item/cac:ClassifiedTaxCategory/cbc:Percent"), "a standard-rated line has no VAT rate"),
    ("line_category_invalid", "tax.edi_tax_category", text(f"{LINE1}/cac:Item/cac:ClassifiedTaxCategory/cbc:ID", "Q"), "a line's tax category is not a UNCL5305 code"),
    ("exempt_without_reason", "tax.edi_exemption_reason", exempt_without_reason, "an exempt line has no exemption reason"),
    ("unit_invalid", "line.product_uom_id", attr(f"{LINE1}/cbc:InvoicedQuantity", "unitCode", "PIECES"), "a line's unit of measure has no UN/ECE code"),
    ("line_amount_wrong", "invoice.invoice_line_ids", text(f"{LINE1}/cbc:LineExtensionAmount", "500.00"), "a line amount does not match its quantity and price"),
    ("issue_date_format", "invoice.invoice_date", text("/inv:Invoice/cbc:IssueDate", "10/03/2026"), "the invoice date is not a valid date"),
    ("type_code_invalid", "invoice.move_type", text("/inv:Invoice/cbc:InvoiceTypeCode", "999"), "the invoice type code is not valid"),
    ("seller_registry_scheme", "company.company_registry", attr(f"{SELLER}/cac:PartyLegalEntity/cbc:CompanyID", "schemeID", "XYZ"), "the company's registration number has an unknown scheme"),
    ("buyer_endpoint_and_country", "partner.peppol_endpoint", both(remove(f"{BUYER}/cbc:EndpointID"), text(f"{BUYER}/cac:PostalAddress/cac:Country/cbc:IdentificationCode", "")), "the customer has neither a Peppol address nor a country"),
    ("payment_means_invalid", "bank.acc_number", text("/inv:Invoice/cac:PaymentMeans/cbc:PaymentMeansCode", "999"), "the payment means code is not valid"),
    ("buyer_vat_and_name", "partner.vat", both(text(f"{BUYER}/cac:PartyTaxScheme/cbc:CompanyID", "40303265045"), text(f"{BUYER}/cac:PartyLegalEntity/cbc:RegistrationName", "")), "the customer's VAT number has no prefix and its legal name is empty"),
]
# fmt: on


def findings_of(name: str) -> list[dict[str, str]]:
    from modules.account_einvoice.validator import validate

    mutate = next(m for n, _t, m, _d in MUTATIONS if n == name)
    root = base()
    mutate(root)
    xml = etree.tostring(root, xml_declaration=True, encoding="UTF-8")
    return [f.as_dict() for f in validate(xml)]
