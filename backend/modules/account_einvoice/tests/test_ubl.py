"""UBL 2.1 / Peppol BIS 3 generation validated with the official artefacts (2-h), import, access point."""

from __future__ import annotations

from typing import Any

import pytest
from lxml import etree

from modules.account.chart import chart_ref
from modules.account.tests.common import D, eu_env, invoice, make_tax, partner
from modules.account_einvoice.demo import gln, setup_companies
from modules.account_einvoice.edi import generate_document, poll, send_moves
from modules.account_einvoice.ubl import NS, build_ubl, tax_category
from modules.account_einvoice.ubl_import import create_bill, parse
from modules.account_einvoice.validator import fatal, validate, validate_schematron, validate_xsd
from tessera.exceptions import AccessError, UserError, ValidationError

pytestmark = pytest.mark.db


def _xp(xml: bytes, path: str) -> list[Any]:
    return etree.fromstring(xml).xpath(path, namespaces={"cbc": NS["cbc"], "cac": NS["cac"]})


@pytest.fixture
def eu(env):  # type: ignore[no-untyped-def]
    e = eu_env(env)
    setup_companies(env)
    return e


def _customer(e: Any, name: str, country: str, **vals: Any) -> Any:
    c = e["core.country"].search([("code", "=", country)], limit=1)
    return partner(e, name, country_id=c.ids[0], street="1 Market Street", city="Town", zip="1000", **vals)


def _assert_valid(xml: bytes) -> None:
    findings = validate(xml)
    assert not fatal(findings), [(f.rule_id, f.message) for f in fatal(findings)]


def test_standard_rate_invoice_validates(eu):  # type: ignore[no-untyped-def]
    customer = _customer(
        eu, "Lyon Retail SARL", "FR", vat="FR40303265045", peppol_eas="9957", peppol_endpoint="FR40303265045"
    )
    tax = eu.company.default_sale_tax_id
    inv = invoice(
        eu,
        customer,
        [
            {"name": "Gripper", "quantity": 3, "price_unit": 199.99, "discount": 7.5, "tax_ids": [("set", tax.ids)]},
            {"name": "Installation", "quantity": 1.5, "price_unit": 80.0, "tax_ids": [("set", tax.ids)]},
            {"name": "Goodwill", "quantity": 1, "price_unit": -20.0, "tax_ids": [("set", tax.ids)]},
        ],
        ref="PO-778",
    )
    xml = build_ubl(inv)
    assert not validate_xsd(xml)
    _assert_valid(xml)
    assert _xp(xml, "/*/cbc:CustomizationID/text()") == [
        "urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0"
    ]
    assert _xp(xml, "//cac:AccountingSupplierParty//cbc:EndpointID/@schemeID") == ["9957"]
    assert _xp(xml, "//cac:PayeeFinancialAccount/cbc:ID/text()") == ["FR7630006000011234567890189"]
    assert _xp(xml, "//cac:TaxSubtotal/cac:TaxCategory/cbc:ID/text()") == ["S"]
    assert float(_xp(xml, "//cac:LegalMonetaryTotal/cbc:PayableAmount/text()")[0]) == pytest.approx(inv.amount_total)
    assert _xp(xml, "//cac:Price/cac:AllowanceCharge/cbc:BaseAmount/text()") == ["199.99"]
    assert _xp(xml, "//cbc:BuyerReference/text()") == ["PO-778"]


def test_intra_eu_export_exempt_and_credit_note_validate(eu):  # type: ignore[no-untyped-def]
    berlin = _customer(eu, "Berlin Robotics GmbH", "DE", vat="DE811569869")
    intra = invoice(eu, berlin, [{"price_unit": 500.0, "tax_ids": [("set", eu.company.default_sale_tax_id.ids)]}])
    assert intra.fiscal_position_id  # intra-EU B2B: 0 % intra-community supply
    xml = build_ubl(intra)
    _assert_valid(xml)
    assert _xp(xml, "//cac:TaxSubtotal/cac:TaxCategory/cbc:ID/text()") == ["K"]
    assert _xp(xml, "//cac:TaxCategory/cbc:TaxExemptionReasonCode/text()") == ["VATEX-EU-IC"]
    assert _xp(xml, "//cac:Delivery/cac:DeliveryLocation/cac:Address/cac:Country/cbc:IdentificationCode/text()") == [
        "DE"
    ]
    assert _xp(xml, "//cac:AccountingCustomerParty//cbc:EndpointID/@schemeID") == ["9930"]  # from the VAT number
    boston = _customer(eu, "Boston Labs Inc", "US", peppol_eas="0088", peppol_endpoint=gln("123456789012"))
    export = invoice(eu, boston, [{"price_unit": 900.0, "tax_ids": [("set", eu.company.default_sale_tax_id.ids)]}])
    xml = build_ubl(export)
    _assert_valid(xml)
    assert _xp(xml, "//cac:TaxSubtotal/cac:TaxCategory/cbc:ID/text()") == ["G"]
    exempt = make_tax(
        eu, "Exempt training", 0.0, company=eu.company, edi_tax_category="E", edi_exemption_reason="Vocational training"
    )
    paris = _customer(eu, "Paris School", "FR", vat="FR40303265045")
    mixed = invoice(
        eu,
        paris,
        [
            {"price_unit": 300.0, "tax_ids": [("set", exempt.ids)]},
            {"price_unit": 100.0, "tax_ids": [("set", eu.company.default_sale_tax_id.ids)]},
        ],
    )
    xml = build_ubl(mixed)
    _assert_valid(xml)
    assert sorted(_xp(xml, "//cac:TaxSubtotal/cac:TaxCategory/cbc:ID/text()")) == ["E", "S"]
    refund_ids = mixed.action_reverse(reason="returned", mode="refund")
    refund = eu["account.move"].browse(refund_ids)
    refund.action_post()
    xml = build_ubl(refund)
    assert etree.QName(etree.fromstring(xml)).localname == "CreditNote"
    _assert_valid(xml)
    assert _xp(xml, "//cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID/text()") == [mixed.name]


def test_foreign_currency_invoice_reports_tax_in_accounting_currency(eu):  # type: ignore[no-untyped-def]
    usd = eu["core.currency"].search([("name", "=", "USD")], limit=1)
    customer = _customer(eu, "Nantes Import", "FR", vat="FR40303265045")
    inv = invoice(
        eu,
        customer,
        [{"price_unit": 110.0, "tax_ids": [("set", eu.company.default_sale_tax_id.ids)]}],
        currency_id=usd.ids[0],
        invoice_date=D(2026, 1, 15),
    )
    xml = build_ubl(inv)
    _assert_valid(xml)
    assert _xp(xml, "/*/cbc:TaxCurrencyCode/text()") == ["EUR"]
    assert len(_xp(xml, "/*/cac:TaxTotal")) == 2


def test_validation_reports_structured_findings(eu):  # type: ignore[no-untyped-def]
    customer = _customer(eu, "Broken Buyer", "FR", vat="FR40303265045")
    xml = build_ubl(
        invoice(eu, customer, [{"price_unit": 10.0, "tax_ids": [("set", eu.company.default_sale_tax_id.ids)]}])
    )
    root = etree.fromstring(xml)
    root.remove(root.find(f"{{{NS['cbc']}}}BuyerReference"))
    for amount in root.iter(f"{{{NS['cbc']}}}PayableAmount"):
        amount.text = "999.00"
    findings = validate(etree.tostring(root))
    ids = {f.rule_id for f in fatal(findings)}
    assert "PEPPOL-EN16931-R003" in ids and "BR-CO-16" in ids
    finding = next(f for f in findings if f.rule_id == "BR-CO-16")
    assert finding.source == "CEN-EN16931" and finding.location and finding.message
    swapped = xml.replace(b"<cbc:ProfileID>", b"<cbc:Unknown>x</cbc:Unknown><cbc:ProfileID>")
    assert validate_xsd(swapped) and validate(swapped)[0].source == "XSD"
    assert validate(b"not xml")[0].rule_id == "XML"
    assert validate_xsd(b"<Order xmlns='urn:x'/>")[0].rule_id == "UBL-ROOT"
    assert validate_schematron(xml) == []


def test_send_receive_round_trip(eu, env):  # type: ignore[no-untyped-def]
    """2-h: the mock access point round-trips send / delivery status / receive."""
    us = env.ref("core.main_company")
    customer = _customer(eu, "My Company US", "US", peppol_eas=us.peppol_eas, peppol_endpoint=us.peppol_endpoint)
    inv = invoice(
        eu, customer, [{"price_unit": 250.0, "quantity": 2, "tax_ids": [("set", eu.company.default_sale_tax_id.ids)]}]
    )
    result = inv.action_send_peppol()
    doc = inv.edi_document_ids
    assert result["documents"][0]["state"] == "sent" and doc.transmission_id.startswith("MOCK-")
    assert doc.attachment_id and doc.receiver == f"0088:{us.peppol_endpoint}" and inv.edi_state == "sent"
    with pytest.raises(UserError):
        inv.action_send_peppol()  # already sent
    assert poll(eu.company) == {"updated": 1, "received": 0}
    assert doc.state == "delivered" and inv.edi_state == "delivered"
    received = poll(us)
    assert received["received"] == 1
    bill_doc = env["account.edi_document"].search(
        [("transmission_id", "=", doc.transmission_id), ("direction", "=", "in")]
    )
    bill = bill_doc.move_id
    assert bill.move_type == "in_invoice" and bill.state == "draft" and bill.company_id == us
    assert bill.ref == inv.name and bill.amount_untaxed == pytest.approx(inv.amount_untaxed)
    assert bill.partner_id.peppol_endpoint == eu.company.peppol_endpoint
    assert poll(us)["received"] == 0  # collected once


def test_invalid_document_is_not_sent_and_rejection(eu):  # type: ignore[no-untyped-def]
    nowhere = _customer(eu, "No Address Ltd", "GB")
    inv = invoice(eu, nowhere, [{"price_unit": 10.0}])
    send_moves(inv)
    doc = inv.edi_document_ids
    assert doc.state == "error" and doc.errors[0]["rule_id"] == "TESSERA-DATA" and doc.error_count == 1
    rejecting = _customer(eu, "Reject Co", "FR", vat="FR40303265045", peppol_eas="9957", peppol_endpoint="FRreject")
    inv2 = invoice(eu, rejecting, [{"price_unit": 10.0, "tax_ids": [("set", eu.company.default_sale_tax_id.ids)]}])
    send_moves(inv2)
    poll(eu.company)
    assert inv2.edi_document_ids.state == "rejected"
    # fix the address and send again from the document
    rejecting.write({"peppol_endpoint": "FR40303265045"})
    assert inv2.edi_document_ids.action_retry() == "sent"


def test_generate_only_and_draft_refused(eu):  # type: ignore[no-untyped-def]
    customer = _customer(eu, "Generate Only", "FR", vat="FR40303265045")
    inv = invoice(eu, customer, [{"price_unit": 42.0, "tax_ids": [("set", eu.company.default_sale_tax_id.ids)]}])
    result = inv.action_generate_ubl()
    assert result["state"] == "to_send" and result["attachment_id"]
    draft = invoice(eu, customer, [{"price_unit": 1.0}], post=False)
    assert generate_document(draft).state == "error"


def test_import_bill_with_reverse_charge_and_allowance(eu, env):  # type: ignore[no-untyped-def]
    us = env.ref("core.main_company")
    xml = b"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
 xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
 xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2">
 <cbc:ID>DE-2026-77</cbc:ID><cbc:IssueDate>2026-03-01</cbc:IssueDate><cbc:DueDate>2026-03-31</cbc:DueDate>
 <cbc:DocumentCurrencyCode>EUR</cbc:DocumentCurrencyCode>
 <cac:AccountingSupplierParty><cac:Party><cbc:EndpointID schemeID="9930">DE811569869</cbc:EndpointID>
  <cac:PostalAddress><cac:Country><cbc:IdentificationCode>DE</cbc:IdentificationCode></cac:Country></cac:PostalAddress>
  <cac:PartyTaxScheme><cbc:CompanyID>DE811569869</cbc:CompanyID><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:PartyTaxScheme>
  <cac:PartyLegalEntity><cbc:RegistrationName>Munich Parts AG</cbc:RegistrationName></cac:PartyLegalEntity></cac:Party></cac:AccountingSupplierParty>
 <cac:AllowanceCharge><cbc:ChargeIndicator>true</cbc:ChargeIndicator><cbc:AllowanceChargeReason>Freight</cbc:AllowanceChargeReason><cbc:Amount currencyID="EUR">25.00</cbc:Amount>
  <cac:TaxCategory><cbc:ID>AE</cbc:ID><cbc:Percent>0</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:TaxCategory></cac:AllowanceCharge>
 <cac:LegalMonetaryTotal><cbc:PayableAmount currencyID="EUR">425.00</cbc:PayableAmount></cac:LegalMonetaryTotal>
 <cac:InvoiceLine><cbc:ID>1</cbc:ID><cbc:InvoicedQuantity unitCode="C62">4</cbc:InvoicedQuantity><cbc:LineExtensionAmount currencyID="EUR">400.00</cbc:LineExtensionAmount>
  <cac:Item><cbc:Name>Servo motor</cbc:Name><cac:ClassifiedTaxCategory><cbc:ID>AE</cbc:ID><cbc:Percent>0</cbc:Percent><cac:TaxScheme><cbc:ID>VAT</cbc:ID></cac:TaxScheme></cac:ClassifiedTaxCategory></cac:Item>
  <cac:Price><cbc:PriceAmount currencyID="EUR">100.00</cbc:PriceAmount></cac:Price></cac:InvoiceLine>
</Invoice>"""
    data = parse(xml)
    assert data["supplier"]["eas"] == "9930" and len(data["lines"]) == 2
    bill = create_bill(eu.company, xml)
    assert bill.partner_id.name == "Munich Parts AG" and bill.partner_id.peppol_endpoint == "DE811569869"
    assert bill.amount_untaxed == pytest.approx(425.0) and bill.invoice_date_due == D(2026, 3, 31)
    rc = chart_ref(eu.company, "purchase_vat_20_rc")
    assert bill.invoice_line_ids[0].tax_ids == rc
    again = create_bill(eu.company, xml)
    assert again.partner_id == bill.partner_id  # found by its Peppol address the second time
    with pytest.raises(UserError):
        parse(b"<Order xmlns='urn:x'/>")
    with pytest.raises(UserError):
        parse(b"<<")
    assert us


def test_tax_category_rules(eu):  # type: ignore[no-untyped-def]
    assert tax_category(eu.company.default_sale_tax_id).category == "S"
    assert tax_category(chart_ref(eu.company, "sale_vat_0_export")).reason_code == "VATEX-EU-G"
    zero = make_tax(eu, "Zero books", 0.0, company=eu.company)
    assert tax_category(zero).category == "Z"
    assert tax_category(eu["account.tax"].browse([])).category == "E"
    reverse = make_tax(eu, "Reverse charge services", 0.0, company=eu.company)
    assert tax_category(reverse).category == "AE"


def test_peppol_fields_access_and_isolation(eu, env):  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        partner(eu, "Bad scheme", peppol_eas="12")
    with pytest.raises(ValidationError):
        eu.company.write({"peppol_eas": "ABCD"})
    customer = _customer(eu, "Isolation Buyer", "FR", vat="FR40303265045")
    inv = invoice(eu, customer, [{"price_unit": 5.0, "tax_ids": [("set", eu.company.default_sale_tax_id.ids)]}])
    send_moves(inv)
    user = env["core.user"].create(
        {
            "name": "us einvoice",
            "login": "einvoice-us@test",
            "company_id": env.ref("core.main_company").ids[0],
            "company_ids": [("set", env.ref("core.main_company").ids)],
            "group_ids": [("set", [env.ref("account.group_account_user").ids[0]])],
        }
    )
    us = env.with_user(user.ids[0])
    assert not us["account.edi_document"].search([("move_id", "=", inv.ids[0])])
    with pytest.raises(AccessError):
        us["account.edi_mock_transmission"].search([])


def test_compiled_peppol_rules_are_reproducible(tmp_path):  # type: ignore[no-untyped-def]
    from modules.account_einvoice.validator import ROOT, compile_schematron

    out = compile_schematron(ROOT / "schematron" / "PEPPOL-EN16931-UBL.sch", tmp_path / "peppol.xslt")
    vendored = (ROOT / "schematron" / "PEPPOL-EN16931-UBL.xslt").read_bytes()
    assert out.read_bytes().count(b"PEPPOL-EN16931-R") == vendored.count(b"PEPPOL-EN16931-R") > 50


def test_demo(env):  # type: ignore[no-untyped-def]
    from modules.account_einvoice.demo import seed

    result = seed(env)
    assert result is not None and result["state"] == "delivered" and result["received"] == 1
    assert seed(env) is None
