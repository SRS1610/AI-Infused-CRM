"""The sub-phase 2.3 views, actions and menus load and resolve (including extensions of other modules' views)."""

from __future__ import annotations

import json

import pytest

from modules.web.views import resolve_view

pytestmark = pytest.mark.db

VIEWS = [
    ("account.bank_statement", "form", "account_bank.view_bank_statement_form"),
    ("account.bank_statement", "list", "account_bank.view_bank_statement_list"),
    ("account.bank_statement_line", "form", "account_bank.view_bank_statement_line_form"),
    ("account.reconcile_model", "form", "account_bank.view_reconcile_model_form"),
    ("account.bank_statement_import", "form", "account_bank.view_statement_import_form"),
    ("payment.provider", "form", "payment.view_provider_form"),
    ("payment.transaction", "form", "payment.view_transaction_form"),
    ("account.dunning_run", "form", "account_dunning.view_dunning_run_form"),
    ("account.dunning_level", "form", "account_dunning.view_dunning_level_form"),
    ("account.edi_document", "form", "account_einvoice.view_edi_document_form"),
]


@pytest.mark.parametrize("model,view_type,ref", VIEWS)
def test_views_resolve(env, model, view_type, ref):  # type: ignore[no-untyped-def]
    definition = resolve_view(env, model, view_type, ref)
    assert definition


def test_extensions_of_other_views(env):  # type: ignore[no-untyped-def]
    invoice_form = json.dumps(resolve_view(env, "account.move", "form", "account.view_invoice_form"))
    assert "action_send_peppol" in invoice_form
    partner_form = json.dumps(resolve_view(env, "contacts.partner", "form"))
    assert "peppol_endpoint" in partner_form and "dunning_excluded" in partner_form
    company_form = json.dumps(resolve_view(env, "core.company", "form"))
    assert "peppol_eas" in company_form and "dunning_min_amount" in company_form


def test_client_action_and_menus(env):  # type: ignore[no-untyped-def]
    for xmlid in (
        "account_bank.menu_bank_reconcile",
        "account_bank.menu_reconcile_models",
        "payment.menu_transactions",
        "payment.menu_providers",
        "account_dunning.menu_dunning_runs",
        "account_einvoice.menu_edi_documents",
    ):
        assert env.ref(xmlid, raise_if_not_found=False) is not None, xmlid
    action = env.ref("account_bank.action_bank_reconcile")
    assert action.client_tag == "account_bank.reconcile"
