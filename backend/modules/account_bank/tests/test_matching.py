"""Posting and matching bank transactions: invoices, payments, write-offs, partial matches, undo."""

from __future__ import annotations

import pytest

from modules.account.tests.common import D, eu_env, invoice, make_tax, partner
from modules.account_bank.tests.common import bank_journal, statement
from tessera.exceptions import UserError

pytestmark = pytest.mark.db


def _items(st_line):
    move = st_line.move_id
    return {line.account_id.code: float(line.balance) for line in move.line_ids}


def test_posting_books_bank_against_suspense(env):
    st = statement(env, [{"amount": 120.0, "payment_ref": "Deposit", "date": D(2026, 3, 12)}])
    line = st.line_ids
    assert st.state == "posted" and line.state == "posted"
    assert line.move_id.state == "posted" and line.move_id.statement_line_id == line
    assert _items(line) == {"1000": 120.0, "1040": -120.0}
    assert line.amount_residual == 120.0
    with pytest.raises(UserError):
        line.write({"amount": 50.0})
    with pytest.raises(UserError):
        line.unlink()
    with pytest.raises(UserError):
        st.unlink()


def test_statement_balance_must_add_up(env):
    journal = bank_journal(env)
    st = env["account.bank_statement"].create(
        {
            "journal_id": journal.ids[0],
            "balance_start": 0.0,
            "balance_end_real": 99.0,
            "line_ids": [("create", {"payment_ref": "x", "amount": 100.0})],
        }
    )
    assert st.difference == pytest.approx(-1.0)
    with pytest.raises(UserError):
        st.action_post()
    # lines stay editable before posting
    st.line_ids.write({"amount": 99.0})
    st.action_post()
    assert st.name.startswith("BNK1 ")


def test_match_invoice_fully(env):
    customer = partner(env, "Bank Customer")
    inv = invoice(env, customer, [{"price_unit": 300.0}])
    st = statement(env, [{"amount": 300.0, "payment_ref": f"Payment {inv.name}", "date": D(2026, 3, 20)}])
    line = st.line_ids
    result = line.action_match(
        items=[{"line_id": inv.line_ids.filtered(lambda r: r.display_type == "payment_term").ids[0]}]
    )
    assert result["state"] == "reconciled"
    assert line.state == "reconciled" and line.is_reconciled and line.amount_residual == 0
    assert inv.payment_state == "paid" and inv.amount_residual == 0
    assert _items(line) == {"1000": 300.0, "1100": -300.0}
    assert line.move_id.name.startswith("BNK1/")  # number kept through draft → post
    # undo restores the suspense item and the invoice
    line.action_unmatch()
    assert line.state == "posted" and inv.payment_state == "not_paid"
    assert _items(line) == {"1000": 300.0, "1040": -300.0}


def test_partial_match_leaves_remainders_open(env):
    customer = partner(env, "Partial Customer")
    inv = invoice(env, customer, [{"price_unit": 500.0}])
    term = inv.line_ids.filtered(lambda r: r.display_type == "payment_term")
    small = statement(env, [{"amount": 200.0, "payment_ref": inv.name}]).line_ids
    small.action_match(items=[{"line_id": term.ids[0]}])
    assert small.state == "reconciled"
    assert inv.payment_state == "partial" and inv.amount_residual == pytest.approx(300.0)
    big = statement(env, [{"amount": 400.0, "payment_ref": inv.name}]).line_ids
    big.action_match(items=[{"line_id": term.ids[0]}])
    assert inv.payment_state == "paid"
    assert big.state == "posted" and big.amount_residual == pytest.approx(100.0)  # 100 still on suspense
    items = _items(big)
    assert items["1040"] == pytest.approx(-100.0) and items["1100"] == pytest.approx(-300.0)
    # explain the rest with a write-off: the earlier match is kept
    other = env["account.account"].search([("code", "=", "4900"), ("company_id", "=", env.company.ids[0])])
    big.action_match(writeoffs=[{"account_id": other.ids[0], "label": "Overpayment kept"}])
    assert big.state == "reconciled"
    assert "1040" not in _items(big) and inv.payment_state == "paid"


def test_explicit_partial_amount_and_credit_note(env):
    customer = partner(env, "Netting Customer")
    inv = invoice(env, customer, [{"price_unit": 100.0}])
    refund = invoice(env, customer, [{"price_unit": 30.0}], move_type="out_refund")
    term = inv.line_ids.filtered(lambda r: r.display_type == "payment_term")
    refund_term = refund.line_ids.filtered(lambda r: r.display_type == "payment_term")
    line = statement(env, [{"amount": 70.0, "payment_ref": "net"}]).line_ids
    line.action_match(items=[{"line_id": refund_term.ids[0]}, {"line_id": term.ids[0]}])
    assert line.state == "reconciled"
    assert inv.payment_state == "paid" and refund.payment_state == "paid"
    line2 = statement(env, [{"amount": 10.0, "payment_ref": "x"}]).line_ids
    inv2 = invoice(env, customer, [{"price_unit": 50.0}])
    term2 = inv2.line_ids.filtered(lambda r: r.display_type == "payment_term")
    line2.action_match(items=[{"line_id": term2.ids[0], "amount": 5.0}])
    assert inv2.amount_residual == pytest.approx(45.0) and line2.amount_residual == pytest.approx(5.0)


def test_writeoff_with_tax(env):
    tax = make_tax(env, "Bank VAT 20", 20.0, use="purchase")
    fees = env["account.account"].search([("code", "=", "6700"), ("company_id", "=", env.company.ids[0])])
    line = statement(env, [{"amount": -12.0, "payment_ref": "Card fees"}]).line_ids
    line.action_match(writeoffs=[{"account_id": fees.ids[0], "tax_id": tax.ids[0], "label": "Card fees"}])
    assert line.state == "reconciled"
    by_account = _items(line)
    assert by_account["6700"] == pytest.approx(10.0) and by_account["1410"] == pytest.approx(2.0)
    tax_item = line.move_id.line_ids.filtered(lambda r: r.tax_line_id)
    assert tax_item.tax_line_id == tax


def test_match_outstanding_payment_marks_invoice_paid(env):
    journal = bank_journal(env)
    journal.write({"payment_through_outstanding": True})
    customer = partner(env, "Outstanding Customer")
    inv = invoice(env, customer, [{"price_unit": 80.0}])
    res = inv.action_register_payment(journal_id=journal.ids[0], date="2026-03-15")
    payment = env["account.payment"].browse(res["payment_ids"])
    assert inv.payment_state == "in_payment" and not payment.is_matched
    outstanding = payment.move_id.line_ids.filtered(lambda r: r.account_id.code == "1020")
    line = statement(env, [{"amount": 80.0, "payment_ref": inv.name}]).line_ids
    line.action_match(items=[{"line_id": outstanding.ids[0]}])
    assert payment.is_matched and inv.payment_state == "paid"
    line.action_unmatch()
    assert not payment.is_matched and inv.payment_state == "in_payment"


def test_vendor_bill_payment(env):
    vendor = partner(env, "Bank Vendor")
    bill = invoice(env, vendor, [{"price_unit": 45.0}], move_type="in_invoice")
    term = bill.line_ids.filtered(lambda r: r.display_type == "payment_term")
    line = statement(env, [{"amount": -45.0, "payment_ref": "bill"}]).line_ids
    line.action_match(items=[{"line_id": term.ids[0]}])
    assert bill.payment_state == "paid" and line.state == "reconciled"


def test_foreign_currency_invoice_books_exchange_difference(env):
    e = eu_env(env)
    usd = e["core.currency"].search([("name", "=", "USD")], limit=1)
    customer = partner(e, "Boston Buyer")
    inv = invoice(e, customer, [{"price_unit": 110.0}], currency_id=usd.ids[0], invoice_date=D(2026, 1, 15))
    assert inv.amount_residual_signed == pytest.approx(100.0)  # 110 USD at 1.10
    term = inv.line_ids.filtered(lambda r: r.display_type == "payment_term")
    line = statement(
        e, [{"amount": 104.76, "payment_ref": inv.name, "date": D(2026, 3, 20)}], journal=bank_journal(e)
    ).line_ids
    line.action_match(items=[{"line_id": term.ids[0]}])
    assert inv.payment_state == "paid" and line.state == "reconciled"
    exchange = term.full_reconcile_id.exchange_move_id
    assert exchange and exchange.state == "posted"


def test_matching_errors(env):
    customer = partner(env, "Err Customer")
    inv = invoice(env, customer, [{"price_unit": 10.0}])
    line = statement(env, [{"amount": 10.0}]).line_ids
    income = inv.line_ids.filtered(lambda r: r.display_type == "product")
    with pytest.raises(UserError):
        line.action_match(items=[{"line_id": income.ids[0]}])  # not reconcilable
    with pytest.raises(UserError):
        line.action_match()
    e = eu_env(env)
    other = invoice(e, partner(e, "EU Err"), [{"price_unit": 10.0}])
    with pytest.raises(UserError):
        line.action_match(
            items=[{"line_id": other.line_ids.filtered(lambda r: r.display_type == "payment_term").ids[0]}]
        )


def test_draft_line_is_posted_when_matched(env):
    customer = partner(env, "Draft Line Customer")
    inv = invoice(env, customer, [{"price_unit": 25.0}])
    st = statement(env, [{"amount": 25.0}], post=False)
    line = st.line_ids
    assert line.state == "draft"
    line.action_match(items=[{"line_id": inv.line_ids.filtered(lambda r: r.display_type == "payment_term").ids[0]}])
    assert line.state == "reconciled" and inv.payment_state == "paid"
    info = line.get_reconcile_info()
    assert info["counterparts"][0]["matched"][0]["move"] == inv.name
