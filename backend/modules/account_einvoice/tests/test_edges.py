"""E-invoicing edge cases: hooks, scheduled poll, access-point errors, unreadable inbound documents."""

from __future__ import annotations

import pytest

from modules.account.tests.common import eu_env, invoice, partner
from modules.account_einvoice.access_point import AccessPointError, OutboundDocument, adapter_for
from modules.account_einvoice.access_point.mock import MockAccessPoint
from modules.account_einvoice.demo import setup_companies
from modules.account_einvoice.edi import poll, poll_job, send_moves
from modules.account_einvoice.hooks import post_init, tag_chart_taxes

pytestmark = pytest.mark.db


def test_chart_taxes_are_tagged(env):
    e = eu_env(env)
    post_init(env)
    assert tag_chart_taxes(e.company) == 0  # already done at installation
    eur = env["core.currency"].search([("name", "=", "EUR")], limit=1)
    fr = env["core.country"].search([("code", "=", "FR")], limit=1)
    company = env["core.company"].create({"name": "New EU Co", "currency_id": eur.ids[0], "country_id": fr.ids[0]})
    from modules.account.chart import chart_ref

    assert chart_ref(company, "sale_vat_0_eu").elevated().edi_tax_category == "K"


def test_mock_access_point_errors_and_unknown_transmission(env):
    e = eu_env(env)
    adapter = MockAccessPoint(e.company)
    with pytest.raises(AccessPointError):
        adapter.send(
            OutboundDocument(sender="9957:FR1", receiver="", document_type="Invoice", payload=b"<x/>", reference="R")
        )
    assert adapter.status("MOCK-NOPE").state == "error"
    assert (
        MockAccessPoint(
            env["core.company"].create({"name": "No Peppol Co", "currency_id": env.company.currency_id.ids[0]})
        ).receive()
        == []
    )
    e.company.elevated().write({"peppol_access_point": None})
    assert isinstance(adapter_for(e.company), MockAccessPoint)


def test_unreadable_inbound_document_is_logged(env):
    e = eu_env(env)
    setup_companies(env)
    us = env.ref("core.main_company")
    adapter = MockAccessPoint(e.company)
    tid = adapter.send(
        OutboundDocument(
            sender=f"{e.company.peppol_eas}:{e.company.peppol_endpoint}",
            receiver=f"{us.peppol_eas}:{us.peppol_endpoint}",
            document_type="Invoice",
            payload=b"<Order xmlns='urn:x'/>",
            reference="BROKEN",
        )
    )
    adapter.status(tid)
    assert poll(us)["received"] == 0
    doc = env["account.edi_document"].search([("transmission_id", "=", tid)])
    assert doc.direction == "in" and doc.state == "error" and doc.errors[0]["rule_id"] == "IMPORT"


def test_poll_job_and_retry_states(env):
    e = eu_env(env)
    setup_companies(env)
    customer = partner(
        e,
        "Job Buyer",
        vat="FR40303265045",
        country_id=env["core.country"].search([("code", "=", "FR")], limit=1).ids[0],
    )
    inv = invoice(e, customer, [{"price_unit": 15.0, "tax_ids": [("set", e.company.default_sale_tax_id.ids)]}])
    send_moves(inv)
    result = poll_job(env)
    assert result[e.company.name]["updated"] >= 1
    doc = inv.edi_document_ids
    assert doc.state == "delivered" and doc.action_retry() == "delivered"  # nothing to retry
    assert doc.display_name.startswith(inv.name)
