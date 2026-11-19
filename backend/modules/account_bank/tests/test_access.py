"""Access rights, company isolation and the import endpoint."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from modules.account.tests.common import eu_env, invoice, partner
from modules.account_bank.tests.common import bank_journal, sample, statement
from tessera.exceptions import AccessError

pytestmark = pytest.mark.db


def _user(env: Any, login: str, groups: list[str], company: Any | None = None) -> Any:
    company = company or env.company
    return env["core.user"].create(
        {
            "name": login,
            "login": login,
            "company_id": company.ids[0],
            "company_ids": [("set", company.ids)],
            "group_ids": [("set", [env.ref(g).ids[0] for g in groups])],
        }
    )


def test_accountant_reconciles_billing_user_cannot(env):
    customer = partner(env, "Access Customer")
    inv = invoice(env, customer, [{"price_unit": 20.0}])
    line = statement(env, [{"amount": 20.0, "payment_ref": inv.name}]).line_ids
    clerk = env.with_user(_user(env, "bank-billing@test", ["account.group_account_invoice"]).ids[0])
    with pytest.raises(AccessError):
        clerk["account.bank_statement_line"].search([])
    auditor = env.with_user(_user(env, "bank-auditor@test", ["account.group_account_readonly"]).ids[0])
    assert auditor["account.bank_statement_line"].browse(line.ids).payment_ref == inv.name
    with pytest.raises(AccessError):
        auditor["account.bank_statement_line"].browse(line.ids).action_match(
            items=[{"line_id": inv.line_ids.filtered(lambda r: r.display_type == "payment_term").ids[0]}]
        )
    accountant = env.with_user(_user(env, "bank-accountant@test", ["account.group_account_user"]).ids[0])
    acc_line = accountant["account.bank_statement_line"].browse(line.ids)
    assert acc_line.get_suggestions()[0]["line_ids"]
    acc_line.action_match(items=[{"line_id": inv.line_ids.filtered(lambda r: r.display_type == "payment_term").ids[0]}])
    assert inv.payment_state == "paid"
    with pytest.raises(AccessError):
        accountant["account.reconcile_model"].create({"name": "Nope"})  # rules are configured by advisers


def test_company_isolation(env):
    e = eu_env(env)
    eu_line = statement(e, [{"amount": 5.0, "payment_ref": "EU only"}], journal=bank_journal(e)).line_ids
    us_user = env.with_user(
        _user(env, "bank-us@test", ["account.group_account_user"], env.ref("core.main_company")).ids[0]
    )
    assert not us_user["account.bank_statement_line"].search([("id", "=", eu_line.ids[0])])
    assert not us_user["account.bank_statement"].search([("id", "=", eu_line.statement_id.ids[0])])
    assert not us_user["account.reconcile_model"].search([("company_id", "=", e.company.ids[0])])
    with pytest.raises(AccessError):
        us_user["account.bank_statement_line"].browse(eu_line.ids).read(["payment_ref"])
    assert not us_user["account.bank_statement_line"].get_reconcile_lines(show="all", query="EU only")


def test_import_endpoint(admin):  # type: ignore[no-untyped-def]
    journals = admin.get(
        "/api/v1/models/account.journal",
        params={"domain": '[["code", "=", "BNK1"], ["company_id.name", "=", "My Company"]]', "fields": "id"},
    ).json()
    journal_id = journals["items"][0]["id"]
    # API tests commit: make the transaction ids unique so the test can run again on a kept database
    content = sample("sample_ofx_v1.ofx").replace(b"<FITID>OFX-", f"<FITID>{uuid.uuid4().hex[:8]}-".encode())
    upload = admin.post("/api/v1/attachments", files={"file": ("ofx-march.ofx", content, "application/x-ofx")}).json()
    res = admin.post("/api/v1/account_bank/import", json={"journal_id": journal_id, "attachment_id": upload["id"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["format"] == "ofx" and body["lines"] == 4 and body["duplicates"] == 0
    again = admin.post(
        "/api/v1/account_bank/import", json={"journal_id": journal_id, "attachment_id": upload["id"]}
    ).json()
    assert again["lines"] == 0 and again["duplicates"] == 4
    missing = admin.post("/api/v1/account_bank/import", json={"journal_id": journal_id, "attachment_id": 999999})
    assert missing.status_code == 404
    lines = admin.post(
        "/api/v1/models/account.bank_statement_line/call/get_reconcile_lines", json={"kwargs": {"show": "open"}}
    )
    assert lines.status_code == 200 and len(lines.json()["result"]) >= 3  # the bank fee is reconciled by its rule
