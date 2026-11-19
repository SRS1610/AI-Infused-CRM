"""Edge cases: rule counterpart types, hooks, journals without accounts, model-level helpers."""

from __future__ import annotations

import pytest

from modules.account.tests.common import invoice, partner
from modules.account_bank.hooks import post_init
from modules.account_bank.matching import line_info, unmatch_line
from modules.account_bank.rules import Candidate, auto_reconcile, suggest_matches
from modules.account_bank.tests.common import bank_journal, statement
from tessera.exceptions import UserError, ValidationError

pytestmark = pytest.mark.db


def _account(env, code):  # type: ignore[no-untyped-def]
    return env["account.account"].search([("code", "=", code), ("company_id", "=", env.company.ids[0])])


def test_rule_counterparts_fixed_and_regex(env):
    fees = _account(env, "6700")
    other = _account(env, "4900")
    rule = env["account.reconcile_model"].create(
        {
            "name": "Card settlement",
            "rule_type": "writeoff_suggestion",
            "auto_reconcile": True,
            "sequence": 1,
            "match_label": "contains",
            "match_label_param": "settlement",
            "line_ids": [
                ("create", {"account_id": fees.ids[0], "amount_type": "regex", "amount_string": r"FEE (\d+[.,]\d+)"}),
                ("create", {"account_id": other.ids[0], "amount_type": "fixed", "amount": 1000.0, "sequence": 20}),
            ],
        }
    )
    line = statement(env, [{"amount": 97.5, "payment_ref": "Card settlement FEE 2,50"}]).line_ids
    plan = rule._writeoff_values(line, -97.5)
    assert [w["balance"] for w in plan] == [-2.5, -95.0]
    assert auto_reconcile(line)["reconciled"] == 1 and line.reconcile_model_id == rule
    by_account = {r.account_id.code: float(r.balance) for r in line.move_id.line_ids}
    assert by_account == {"1000": 97.5, "6700": -2.5, "4900": -95.0}
    no_fee = statement(env, [{"amount": 10.0, "payment_ref": "Card settlement"}]).line_ids
    assert rule._writeoff_values(no_fee, -10.0)[0]["account_id"] == other.ids[0]
    with pytest.raises(ValidationError):
        env["account.reconcile_model_line"].create(
            {
                "model_id": rule.ids[0],
                "account_id": env["account.account"]
                .elevated()
                .search([("company_id", "!=", env.company.ids[0])], limit=1)
                .ids[0],
            }
        )


def test_post_init_and_new_rules_are_idempotent(env):
    before = env["account.reconcile_model"].search_count([])
    post_init(env)
    assert env["account.reconcile_model"].search_count([]) == before


def test_journal_without_accounts(env):
    journal = env["account.journal"].create({"name": "Petty", "code": "PETY", "type": "cash"})
    st = env["account.bank_statement"].create(
        {
            "journal_id": journal.ids[0],
            "balance_end_real": 5.0,
            "line_ids": [("create", {"payment_ref": "x", "amount": 5.0})],
        }
    )
    with pytest.raises(UserError):
        st.action_post()  # no cash account on the journal
    sale = env["account.journal"].search([("type", "=", "sale"), ("company_id", "=", env.company.ids[0])], limit=1)
    with pytest.raises(ValidationError):
        env["account.bank_statement"].create({"journal_id": sale.ids[0]})
    zero = statement(env, [{"amount": 0.0}], post=False)
    with pytest.raises(UserError):
        zero.action_post()


def test_draft_line_suggestions_and_helpers(env):
    customer = partner(env, "Helper Customer")
    inv = invoice(env, customer, [{"price_unit": 88.0}])
    draft = statement(env, [{"amount": 88.0, "payment_ref": inv.name}], post=False).line_ids
    assert suggest_matches(draft)[0].line_ids == inv.line_ids.filtered(lambda r: r.display_type == "payment_term").ids
    assert line_info(draft)["counterparts"] == []
    unmatch_line(draft)  # nothing to undo on a draft line
    draft.action_post()
    assert draft.state == "posted"
    empty = Candidate(kind="items")
    assert empty.line_ids == [] and empty.to_dict()["items"] == []
    st = draft.statement_id
    assert st.action_auto_reconcile()["reconciled"] == 1 and st.reconciled_count == 1
    assert suggest_matches(draft) == []  # nothing left to explain


def test_partner_detection_needs_a_unique_match(env):
    partner(env, "Twin Name SA")
    partner(env, "Twin Name SA")
    line = statement(env, [{"amount": 5.0, "partner_name": "Twin Name SA"}]).line_ids
    assert not line.partner_id
    assert env["account.bank_statement_line"]._find_partner(None, "ab") is None


def test_lines_inherit_the_statement_journal(env):
    journal = bank_journal(env)
    st = env["account.bank_statement"].create({"journal_id": journal.ids[0], "balance_end_real": 0.0})
    line = env["account.bank_statement_line"].create({"statement_id": st.ids[0], "amount": 3.0, "payment_ref": "x"})
    assert line.journal_id == journal and line.display_name == "x"
    line.unlink()
