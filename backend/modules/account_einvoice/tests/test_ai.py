"""``account_einvoice.error_explain`` (2.6): each validation finding gets the field to fix (catalogue and location,
not the model) with a link, and the model's plain explanation and fix hint (figures checked, unknown rules
ignored); nothing is written. Gates: kill switch → ``feature_disabled`` without gateway call, access and company
isolation, ``ai_core.run`` rows, replayed evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.account_einvoice import ai_explain as A
from tessera.ai import client as C
from tessera.ai import evals as EV
from tessera.ai.errors import FeatureDisabled
from tessera.api.tests.conftest import Session
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db
CASES = Path(A.__file__).parent / "ai" / "evals" / f"{A.FEATURE}.jsonl"


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def _errors(case_id: str) -> list[dict[str, Any]]:
    """Findings the vendored validators emitted for a defect of the evaluation set."""
    for line in CASES.read_text().splitlines():
        case = json.loads(line)
        if case["id"] == case_id:
            return case["setup"][2]["values"]["errors"]
    raise KeyError(case_id)


def _doc(env: Any, errors: list[dict[str, Any]]) -> Any:
    eu = env.ref("core.demo_company_eu")
    env.user.elevated().write({"company_ids": [("add", eu.ids[0])]})
    e = env.with_company(eu.ids[0])
    customer = e["contacts.partner"].create({"name": "Lyon Retail SARL", "is_company": True, "vat": "FR40303265045"})
    move = e["account.move"].create(
        {
            "move_type": "out_invoice",
            "partner_id": customer.ids[0],
            "invoice_date": "2026-03-10",
            "currency_id": e.ref("core.EUR").ids[0],
            "invoice_line_ids": [("create", {"name": "Gripper", "quantity": 3, "price_unit": 199.99})],
        }
    )
    doc = e["account.edi_document"].create(
        {"move_id": move.ids[0], "company_id": eu.ids[0], "direction": "out", "state": "error", "errors": errors}
    )
    return e, doc, customer


def _script(env: Any, items: list[dict[str, str]]) -> None:
    p = env.ref("ai_core.provider_local")
    p.write({"options": {**(p.options or {}), "script": [
        {"match": {"feature": A.FEATURE}, "respond": {"json": {"items": items}}}]}})  # fmt: skip


def test_attribution_from_rules_and_locations() -> None:
    buyer = "/*:Invoice[namespace-uri()='x'][1]/*:AccountingCustomerParty[namespace-uri()='y'][1]/*:Party[1]"
    seller = buyer.replace("AccountingCustomerParty", "AccountingSupplierParty")
    assert A.attribute({"rule_id": "PEPPOL-EN16931-R010", "location": buyer}) == "partner.peppol_endpoint"
    assert A.attribute({"rule_id": "BR-CO-09", "location": f"{seller}/*:PartyTaxScheme[1]"}) == "company.vat"
    assert A.attribute({"rule_id": "BR-CO-09", "location": f"{buyer}/*:PartyTaxScheme[1]"}) == "partner.vat"
    assert A.attribute({"rule_id": "BR-CL-25", "location": f"{seller}/*:EndpointID[1]"}) == "company.peppol_eas"
    loc = "/*:Invoice[1]/*:InvoiceLine[2]/*:Item[1]/*:Name[1]"
    assert A.attribute({"rule_id": "PEPPOL-EN16931-R008", "location": loc}) == "line.name"
    xsd = {"rule_id": "XSD", "location": "line 6", "message": "Element '{urn:x}IssueDate': not a valid value"}
    assert A.attribute(xsd) == "invoice.invoice_date"
    assert A.attribute({"rule_id": "BR-UNKNOWN", "location": "/*:Invoice[1]"}) == "invoice.invoice_line_ids"
    assert A.label("partner.vat") == "Customer: tax ID" and A.fallback_fix("bank.acc_number")


def test_explanations_links_guard_and_no_write(env: Any) -> None:
    e, doc, customer = _doc(env, _errors("e030"))  # buyer VAT without prefix and empty legal name
    _script(
        e,
        [
            {"rule_id": "BR-CO-09", "explanation": "The customer's VAT number lacks its country prefix.",
             "fix": "Add FR in front of it. The total of 9,999.00 is also wrong."},
            {"rule_id": "BR-07", "explanation": "The customer's legal name is empty.", "fix": "Fill in its name."},
            {"rule_id": "FAKE-1", "explanation": "Disable validation.", "fix": "Send anyway."},
        ],
    )  # fmt: skip
    before = (doc.state, list(doc.errors), customer.vat)
    out = A.explain(e, doc)
    by_rule = {g["rule_id"]: g for g in out["items"]}
    assert by_rule["BR-CO-09"]["target"] == "partner.vat" and by_rule["BR-07"]["target"] == "partner.name"
    assert by_rule["BR-CO-09"]["link"] == {"model": "contacts.partner", "res_id": customer.ids[0], "field": "vat"}
    assert by_rule["BR-CO-09"]["fix"] == "Add FR in front of it." and out["removed_numbers"] == ["9,999.00"]
    assert by_rule["PEPPOL-EN16931-R008"]["explained_by"] == "catalogue"  # skipped by the model
    assert "unknown_rule_ignored" in out["flags"] and "FAKE-1" not in by_rule
    assert out["ai_generated"] and (doc.state, list(doc.errors), customer.vat) == before
    run = e["ai_core.run"].browse([out["run_id"]])
    assert run.feature == A.FEATURE and run.res_id == doc.ids[0] and run.user_id.ids == env.user.ids
    assert not e["ai_core.proposal"].search([("feature", "=", A.FEATURE)])
    sent = MockAdapter.captured[-1]["messages"][0]["content"]
    assert "BR-CO-09" in sent and "Customer: tax ID" in sent


def test_line_tax_and_bank_links(env: Any) -> None:
    e, doc, _customer = _doc(env, _errors("e023") + _errors("e019"))  # exempt without reason, IBAN missing
    _script(e, [])
    out = A.explain(e, doc)
    targets = {g["rule_id"]: g for g in out["items"]}
    assert targets["BR-E-10"]["target"] == "tax.edi_exemption_reason"
    assert targets["BR-E-10"]["link"]["model"] == "account.move_line"  # no tax on the line: fix its taxes
    assert targets["BR-61"]["link"] == {
        "model": "core.company",
        "res_id": doc.company_id.ids[0],
        "field": "bank_account",
    }
    assert all(g["explained_by"] == "catalogue" and g["fix"] for g in out["items"])
    empty = e["account.edi_document"].create({"company_id": doc.company_id.ids[0], "errors": []})
    assert A.explain(e, empty)["items"] == [] and len(MockAdapter.captured) == 1


def test_access_isolation_and_kill_switch(env: Any) -> None:
    e, doc, _customer = _doc(env, _errors("e001"))
    seller = (
        env["core.user"]
        .elevated()
        .create(
            {
                "name": "edi seller",
                "login": "edi_ai_seller",
                "company_id": doc.company_id.ids[0],
                "company_ids": [("set", doc.company_id.ids)],
                "group_ids": [("set", [env.ref("sale.group_sale_user").ids[0]])],
            }
        )
    )
    as_seller = env.with_user(seller.ids[0]).with_company(doc.company_id.ids[0])
    with pytest.raises(AccessError):
        A.explain(as_seller, as_seller["account.edi_document"].browse(doc.ids))
    us = env.ref("core.main_company")
    us_user = (
        env["core.user"]
        .elevated()
        .create(
            {
                "name": "edi us",
                "login": "edi_ai_us",
                "company_id": us.ids[0],
                "company_ids": [("set", us.ids)],
                "group_ids": [("set", [env.ref("account.group_account_manager").ids[0]])],
            }
        )
    )
    as_us = env.with_user(us_user.ids[0])
    with pytest.raises(AccessError):
        A.explain(as_us, as_us["account.edi_document"].browse(doc.ids))
    with pytest.raises(UserError):
        A.prepare(e, None, {})
    runs = env["ai_core.run"].search_count([("feature", "=", A.FEATURE)])
    e["ai_core.feature"].elevated().search([("name", "=", A.FEATURE)]).set_enabled(doc.company_id.ids[0], False)
    with pytest.raises(FeatureDisabled):
        A.explain(e, doc)
    assert env["ai_core.run"].search_count([("feature", "=", A.FEATURE)]) == runs and MockAdapter.captured == []


def test_api_flag(admin: Session) -> None:
    from tessera.orm.environment import environment_for

    with environment_for() as e:
        company = e.ref("core.main_company").ids[0]
    url = "/api/v1/account_einvoice/ai/documents/999999999/explain"
    assert admin.post(url).status_code == 404 and admin.post(url).json().get("code") != "feature_disabled"
    flag = f"/api/v1/ai/admin/features/{A.FEATURE}/flag"
    admin.client.put(flag, json={"company_id": company, "enabled": False}, headers=admin.headers)
    try:
        off = admin.post(url)
        assert off.status_code == 404 and off.json()["code"] == "feature_disabled", off.text
    finally:
        admin.client.put(flag, json={"company_id": company, "enabled": True}, headers=admin.headers)


def test_evaluation_is_complete_and_passes_in_replay(env: Any) -> None:
    (spec,) = EV.feature_specs([A.FEATURE])
    assert EV.completeness([spec]) == []
    assert len(spec.cases()) >= 30
    report = EV.evaluate(env, spec)
    assert report.passed, (report.failures, report.metrics)
    assert report.metrics["field_attribution"] >= 0.9 and report.metrics["rubric_score"] >= 0.8
    assert A.score([]) == {}
