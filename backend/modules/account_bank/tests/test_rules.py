"""Reconciliation rules: suggestions, automatic matching and the demo statement (2-d)."""

from __future__ import annotations

import pytest

from modules.account.tests.common import D, eu_env, invoice, partner
from modules.account_bank.rules import auto_reconcile, install_default_models, reference_found, suggest_matches
from modules.account_bank.tests.common import statement
from tessera.exceptions import ValidationError

pytestmark = pytest.mark.db


def _term(move):
    return move.line_ids.filtered(lambda r: r.display_type == "payment_term")


def test_default_rules_are_installed_per_company(env):
    e = eu_env(env)
    for company in (env.company, e.company):
        rules = env["account.reconcile_model"].elevated().search([("company_id", "=", company.ids[0])])
        assert {"Invoices, bills and payments", "Bank fees", "Bank interest"} <= set(rules.mapped("name"))
    assert not install_default_models(env.company)  # idempotent


def test_suggestions_rank_reference_partner_and_amount(env):
    customer = partner(env, "Suggest Customer")
    other = partner(env, "Other Customer")
    target = invoice(env, customer, [{"price_unit": 410.0}])
    decoy = invoice(env, other, [{"price_unit": 410.0}])
    line = statement(
        env, [{"amount": 410.0, "payment_ref": f"Settlement {target.name}", "partner_id": customer.ids[0]}]
    ).line_ids
    cands = suggest_matches(line, limit=5)
    best = cands[0]
    assert best.line_ids == _term(target).ids
    assert {"reference", "partner", "amount_exact"} <= set(best.reasons) and best.score == 1.0
    assert any(c.line_ids == _term(decoy).ids and c.score < best.score for c in cands)
    payload = line.get_suggestions(limit=2)
    assert len(payload) == 2 and payload[0]["items"][0]["move_name"] == target.name
    assert payload[0]["reasons"][0]["message"]


def test_group_suggestion_for_several_invoices(env):
    customer = partner(env, "Group Customer")
    a = invoice(env, customer, [{"price_unit": 100.0}])
    b = invoice(env, customer, [{"price_unit": 250.0}])
    line = statement(env, [{"amount": 350.0, "payment_ref": f"{a.name} {b.name}"}]).line_ids
    best = suggest_matches(line)[0]
    assert set(best.line_ids) == set((_term(a) | _term(b)).ids) and "group" in best.reasons
    assert auto_reconcile(line)["reconciled"] == 1
    assert a.payment_state == "paid" and b.payment_state == "paid"
    assert line.reconcile_model_id.name == "Invoices, bills and payments"


def test_partner_and_amount_without_reference(env):
    customer = partner(env, "Amount Only Customer")
    inv = invoice(env, customer, [{"price_unit": 77.7}])
    line = statement(env, [{"amount": 77.7, "payment_ref": "transfer", "partner_id": customer.ids[0]}]).line_ids
    auto_reconcile(line)
    assert line.state == "reconciled" and inv.payment_state == "paid"
    # the same amount for a partner with two identical invoices is ambiguous: left for review
    twin = partner(env, "Twin Customer")
    invoice(env, twin, [{"price_unit": 55.0}])
    invoice(env, twin, [{"price_unit": 55.0}])
    ambiguous = statement(env, [{"amount": 55.0, "payment_ref": "transfer", "partner_id": twin.ids[0]}]).line_ids
    assert auto_reconcile(ambiguous)["reconciled"] == 0 and ambiguous.state == "posted"


def test_partial_payment_by_reference_and_tolerance(env):
    customer = partner(env, "Tolerance Customer")
    inv = invoice(env, customer, [{"price_unit": 1000.0}])
    line = statement(env, [{"amount": 400.0, "payment_ref": inv.name}]).line_ids
    auto_reconcile(line)
    assert line.state == "reconciled" and inv.payment_state == "partial"
    rule = env.ref(f"account_bank.{env.company.ids[0]}_rule_invoices")
    fees = env["account.account"].search([("code", "=", "6700"), ("company_id", "=", env.company.ids[0])])
    rule.write(
        {
            "allow_payment_tolerance": True,
            "payment_tolerance_type": "fixed_amount",
            "payment_tolerance_param": 5.0,
            "line_ids": [("create", {"account_id": fees.ids[0], "label": "Payment difference"})],
        }
    )
    inv2 = invoice(env, customer, [{"price_unit": 200.0}])
    short = statement(env, [{"amount": 197.0, "payment_ref": inv2.name}]).line_ids
    cands = short.get_suggestions()
    assert "amount_tolerance" in [r["code"] for r in cands[0]["reasons"]] and cands[0]["writeoffs"]
    auto_reconcile(short)
    assert inv2.payment_state == "paid" and short.state == "reconciled"
    assert float(short.move_id.line_ids.filtered(lambda r: r.account_id == fees).balance) == pytest.approx(3.0)
    with pytest.raises(ValidationError):
        rule.write({"payment_tolerance_type": "percentage", "payment_tolerance_param": 150})
    with pytest.raises(ValidationError):
        rule.write({"match_label": "match_regex", "match_label_param": "(unclosed"})


def test_writeoff_rules_bank_fees_and_interest(env):
    lines = statement(
        env,
        [
            {"amount": -7.5, "payment_ref": "Monthly bank fees"},
            {"amount": 3.2, "payment_ref": "Interest credit"},
            {"amount": 3.2, "payment_ref": "Bank fees refund"},  # money in: the fee rule is for money paid
        ],
    ).line_ids.sorted(lambda r: r.amount)
    fee, interest, refund = (
        lines[0],
        lines.filtered(lambda r: r.payment_ref == "Interest credit"),
        lines.filtered(lambda r: r.payment_ref == "Bank fees refund"),
    )
    suggestions = suggest_matches(fee)
    assert suggestions[0].kind == "writeoff" and "label" in suggestions[0].reasons
    assert auto_reconcile(lines)["reconciled"] == 2
    assert fee.state == "reconciled" and interest.state == "reconciled" and refund.state == "posted"
    assert {r.account_id.code for r in fee.move_id.line_ids} == {"1000", "6700"}
    assert {r.account_id.code for r in interest.move_id.line_ids} == {"1000", "4900"}


def test_rule_conditions(env):
    rule = env["account.reconcile_model"].create(
        {
            "name": "Big incoming from Acme",
            "rule_type": "writeoff_suggestion",
            "match_nature": "amount_received",
            "match_amount": "between",
            "match_amount_min": 100,
            "match_amount_max": 200,
            "match_label": "contains",
            "match_label_param": "acme",
            "match_partner": True,
        }
    )
    acme = partner(env, "Acme Rule")
    ok, small, wrong_label, no_partner, outgoing = statement(
        env,
        [
            {"amount": 150.0, "payment_ref": "ACME transfer", "partner_id": acme.ids[0]},
            {"amount": 50.0, "payment_ref": "ACME transfer", "partner_id": acme.ids[0]},
            {"amount": 150.0, "payment_ref": "Globex", "partner_id": acme.ids[0]},
            {"amount": 150.0, "payment_ref": "ACME transfer"},
            {"amount": -150.0, "payment_ref": "ACME transfer", "partner_id": acme.ids[0]},
        ],
    ).line_ids.sorted(lambda r: r.sequence * 0 + r.ids[0])
    assert rule._matches_line(ok)
    assert not any(rule._matches_line(x) for x in (small, wrong_label, no_partner, outgoing))
    rule.write({"match_partner_ids": [("set", [partner(env, "Someone else").ids[0]])]})
    assert not rule._matches_line(ok)
    rule.write(
        {"match_partner": False, "match_label": "not_contains", "match_amount": "greater", "match_amount_min": 10}
    )
    assert not rule._matches_line(ok) and rule._matches_line(wrong_label)
    rule.write({"match_amount": "lower", "match_amount_max": 100})
    assert not rule._matches_line(wrong_label)


def test_search_open_items(env):
    customer = partner(env, "Search Customer")
    inv = invoice(env, customer, [{"price_unit": 61.25}])
    line = statement(env, [{"amount": 61.25, "payment_ref": "?", "partner_id": customer.ids[0]}]).line_ids
    assert [r["id"] for r in line.search_candidates("")] == _term(inv).ids
    assert _term(inv).ids[0] in [r["id"] for r in line.search_candidates(inv.name)]
    assert _term(inv).ids[0] in [r["id"] for r in line.search_candidates("61.25")]
    assert line.search_candidates("Search Customer")[0]["kind"] == "invoice"


def test_reference_detection():
    assert reference_found("INV/2026/00012", "pay inv 2026 00012 thanks", "PAYINV202600012THANKS")
    assert reference_found("4711", "Bill 4711", "BILL4711")
    assert not reference_found("4711", "Bill 47110", "BILL47110")
    assert not reference_found("AB", "AB", "AB")


def test_reconcile_screen_endpoints(env):
    customer = partner(env, "Screen Customer")
    inv = invoice(env, customer, [{"price_unit": 15.0}])
    line = statement(env, [{"amount": 15.0, "payment_ref": inv.name}]).line_ids
    model = env["account.bank_statement_line"]
    rows = model.get_reconcile_lines(journal_id=line.journal_id.ids[0], show="open", query=inv.name)
    assert [r["id"] for r in rows] == line.ids
    result = model.action_auto_reconcile_all(journal_id=line.journal_id.ids[0])
    assert result["reconciled"] >= 1 and line.state == "reconciled"
    assert line.ids[0] in [r["id"] for r in model.get_reconcile_lines(show="reconciled")]
    assert line.statement_id.reconciled_count == 1


def test_demo_statement_reconciles_90_percent(env, system_env):
    """2-d: the demo CAMT.053 statement is reconciled for at least 90 % by the shipped rules."""
    from modules.account.demo import seed_company as seed_accounting
    from modules.account_bank.demo import seed_company as seed_bank

    for company in (env.ref("core.main_company"), eu_env(env).company):
        seed_accounting(system_env, company)
        result = seed_bank(system_env, company)
        assert result is not None and result["format"] == "camt053"
        lines = env["account.bank_statement"].browse(result["statement_ids"]).line_ids
        ratio = result["reconciled"] / result["lines"]
        assert len(lines) == result["lines"] >= 10
        assert ratio >= 0.9, ratio
        assert len(lines.filtered(lambda r: r.state == "reconciled")) == result["reconciled"]
        leftover = lines.filtered(lambda r: r.state != "reconciled")
        assert leftover.mapped("payment_ref") == ["Transfer"]
        demo_invoices = env["account.move"].search(
            [
                ("company_id", "=", company.ids[0]),
                ("ref", "like", "DEMO-"),
                ("move_type", "=", "out_invoice"),
                ("ref", "!=", "DEMO-04"),
            ]
        )
        assert set(demo_invoices.mapped("payment_state")) == {"paid"}
        assert seed_bank(system_env, company) is None  # idempotent


def test_auto_reconcile_job_and_cli(env, tmp_path):
    from typer.testing import CliRunner

    from modules.account_bank.jobs import auto_reconcile_job

    customer = partner(env, "Job Customer")
    inv = invoice(env, customer, [{"price_unit": 33.0}])
    line = statement(env, [{"amount": 33.0, "payment_ref": inv.name}]).line_ids
    assert auto_reconcile_job(env, journal_id=line.journal_id.ids[0])["reconciled"] >= 1
    assert inv.payment_state == "paid"
    from tessera.cli.account import app

    result = CliRunner().invoke(app, ["import-statement", str(tmp_path / "missing.xml"), "--journal", "BNK1"])
    assert result.exit_code != 0


def test_foreign_currency_journal(env):
    e = eu_env(env)
    usd = e["core.currency"].search([("name", "=", "USD")], limit=1)
    company = e.company
    bank_acc = e["account.account"].create(
        {"code": "1001", "name": "USD bank", "account_type": "asset_cash", "company_id": company.ids[0]}
    )
    journal = e["account.journal"].create(
        {
            "name": "USD bank",
            "code": "BUSD",
            "type": "bank",
            "currency_id": usd.ids[0],
            "default_account_id": bank_acc.ids[0],
            "company_id": company.ids[0],
        }
    )
    customer = partner(e, "Dollar Customer")
    inv = invoice(e, customer, [{"price_unit": 210.0}], currency_id=usd.ids[0], invoice_date=D(2026, 1, 15))
    line = statement(e, [{"amount": 210.0, "payment_ref": inv.name, "date": D(2026, 3, 20)}], journal=journal).line_ids
    liquidity = line.move_id.line_ids.filtered(lambda r: r.account_id == bank_acc)
    assert liquidity.currency_id == usd and float(liquidity.amount_currency) == 210.0
    assert float(liquidity.balance) == pytest.approx(200.0)  # 210 USD at 1.05
    auto_reconcile(line)
    assert line.state == "reconciled" and inv.payment_state == "paid"
