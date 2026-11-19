"""Payments, reconciliation, credit notes and exchange differences (2-c)."""

from __future__ import annotations

import pytest

from modules.account.tests.common import D, eu_env, invoice, make_tax, partner
from tessera.exceptions import UserError

pytestmark = pytest.mark.db


@pytest.fixture
def customer(env):
    return partner(env)


def _bank(env):
    return env["account.journal"].search([("type", "=", "bank"), ("company_id", "=", env.company.ids[0])], limit=1)


def test_full_payment_marks_invoice_paid(env, customer):
    inv = invoice(env, customer, [{"price_unit": 250.0}])
    result = inv.action_register_payment(date="2026-03-15")
    payment = env["account.payment"].browse(result["payment_ids"])
    assert payment.state == "posted" and payment.amount == pytest.approx(250.0)
    assert inv.payment_state == "paid"
    assert inv.amount_residual == 0
    assert payment.reconciled_invoice_ids == inv
    assert payment.is_reconciled
    term = inv.line_ids.filtered(lambda r: r.display_type == "payment_term")
    assert term.reconciled and term.full_reconcile_id


def test_partial_payments_then_settlement(env, customer):
    inv = invoice(env, customer, [{"price_unit": 1000.0}])
    inv.action_register_payment(amount=400.0, date="2026-03-12")
    assert inv.payment_state == "partial"
    assert inv.amount_residual == pytest.approx(600.0)
    inv.action_register_payment(date="2026-03-20")
    assert inv.payment_state == "paid"
    with pytest.raises(UserError):
        inv.action_register_payment()


def test_writeoff_closes_the_difference(env, customer):
    inv = invoice(env, customer, [{"price_unit": 100.0}])
    fees = env["account.account"].search([("code", "=", "6700"), ("company_id", "=", env.company.ids[0])])
    inv.action_register_payment(amount=98.5, writeoff_account_id=fees.ids[0], writeoff_label="Bank fees")
    assert inv.payment_state == "paid"
    payment = env["account.payment"].search([("reconciled_invoice_ids", "in", inv.ids)])
    wo = payment.move_id.line_ids.filtered(lambda r: r.account_id == fees)
    assert float(wo.balance) == pytest.approx(1.5)


def test_batch_payment_groups_by_partner(env, customer):
    other = partner(env, "Globex")
    a = invoice(env, customer, [{"price_unit": 100.0}])
    b = invoice(env, customer, [{"price_unit": 50.0}])
    c = invoice(env, other, [{"price_unit": 70.0}])
    result = (a | b | c).action_register_payment(date="2026-03-15")
    payments = env["account.payment"].browse(result["payment_ids"])
    assert sorted(payments.mapped("amount")) == [70.0, 150.0]
    assert {m.payment_state for m in (a | b | c)} == {"paid"}
    with pytest.raises(UserError):
        (a | c).action_register_payment(amount=10.0)


def test_vendor_bill_payment_is_outbound(env):
    vendor = partner(env, "Supplies Ltd")
    bill = invoice(env, vendor, [{"price_unit": 80.0}], move_type="in_invoice")
    result = bill.action_register_payment(date="2026-03-15")
    payment = env["account.payment"].browse(result["payment_ids"])
    assert payment.payment_type == "outbound" and payment.partner_type == "supplier"
    assert bill.payment_state == "paid"


def test_early_payment_discount_adjusts_tax(env, customer):
    tax = make_tax(env, "EPD tax 20%", 20)
    term = env.ref("account.term_2_10_net_30")
    inv = invoice(
        env,
        customer,
        [{"price_unit": 100.0, "tax_ids": [("set", tax.ids)]}],
        payment_term_id=term.ids[0],
        invoice_date=D(2026, 3, 1),
    )
    assert inv.amount_total == pytest.approx(120.0)
    result = inv.action_register_payment(date="2026-03-08")
    payment = env["account.payment"].browse(result["payment_ids"])
    assert payment.amount == pytest.approx(117.6)
    assert inv.payment_state == "paid"
    lines = payment.move_id.line_ids
    discount_acc = env.company.account_epd_discount_expense_id
    assert float(lines.filtered(lambda r: r.account_id == discount_acc).balance) == pytest.approx(2.0)
    assert float(lines.filtered(lambda r: r.tax_line_id == tax).balance) == pytest.approx(0.4)
    # after the window the full amount is due
    late = invoice(
        env,
        customer,
        [{"price_unit": 100.0, "tax_ids": [("set", tax.ids)]}],
        payment_term_id=term.ids[0],
        invoice_date=D(2026, 3, 1),
    )
    result = late.action_register_payment(date="2026-03-20")
    assert env["account.payment"].browse(result["payment_ids"]).amount == pytest.approx(120.0)


def test_credit_note_reversal_modes(env, customer):
    inv = invoice(env, customer, [{"price_unit": 300.0}])
    [refund_id] = inv.action_reverse(reason="damaged", mode="refund")
    refund = env["account.move"].browse([refund_id])
    assert refund.move_type == "out_refund" and refund.state == "draft"
    assert refund.reversed_entry_id == inv
    assert refund.amount_total == pytest.approx(300.0)
    refund.unlink()
    ids = inv.action_reverse(reason="cancelled order", mode="cancel")
    refund = env["account.move"].browse(ids[:1])
    assert refund.state == "posted"
    assert inv.payment_state == "reversed"
    second = invoice(env, customer, [{"price_unit": 40.0}])
    ids = second.action_reverse(mode="modify")
    assert len(ids) == 2
    copy = env["account.move"].browse(ids[1:])
    assert copy.state == "draft" and copy.amount_total == pytest.approx(40.0)


def test_unreconcile_restores_residual(env, customer):
    inv = invoice(env, customer, [{"price_unit": 60.0}])
    inv.action_register_payment()
    term = inv.line_ids.filtered(lambda r: r.display_type == "payment_term")
    term.remove_move_reconcile()
    assert inv.payment_state == "not_paid"
    assert inv.amount_residual == pytest.approx(60.0)


def test_journal_entry_reversal(env):
    misc = env["account.journal"].search([("code", "=", "MISC"), ("company_id", "=", env.company.ids[0])])
    rent = env["account.account"].search([("code", "=", "6100"), ("company_id", "=", env.company.ids[0])])
    accrued = env["account.account"].search([("code", "=", "2200"), ("company_id", "=", env.company.ids[0])])
    move = env["account.move"].create(
        {
            "journal_id": misc.ids[0],
            "date": D(2026, 3, 31),
            "line_ids": [
                ("create", {"account_id": rent.ids[0], "debit": 1200.0, "name": "Rent accrual"}),
                ("create", {"account_id": accrued.ids[0], "credit": 1200.0, "name": "Rent accrual"}),
            ],
        }
    )
    move.action_post()
    assert move.name.startswith("MISC/2026/")
    [rid] = move.action_reverse(date="2026-04-01")
    rev = env["account.move"].browse([rid])
    rev.action_post()
    assert sorted(float(x.balance) for x in rev.line_ids) == [-1200.0, 1200.0]


def test_realised_exchange_difference_on_usd_invoice_paid_in_eur(env):
    """2-c: a USD invoice (rate 1.10) paid in EUR at a later rate (1.05) books a realised exchange gain."""
    e = eu_env(env)
    usd = e["core.currency"].search([("name", "=", "USD")])
    customer = partner(e, "Boston Inc")
    inv = invoice(e, customer, [{"price_unit": 1000.0}], currency_id=usd.ids[0], invoice_date=D(2026, 3, 1))
    term = inv.line_ids.filtered(lambda r: r.display_type == "payment_term")
    assert float(term.balance) == pytest.approx(909.09)  # 1000 / 1.10
    eur_bank = e["account.journal"].search([("type", "=", "bank"), ("company_id", "=", e.company.ids[0])], limit=1)
    result = inv.action_register_payment(
        journal_id=eur_bank.ids[0], date="2026-03-25", currency_id=e.company.currency_id.ids[0]
    )
    payment = e["account.payment"].browse(result["payment_ids"])
    assert payment.amount == pytest.approx(952.38)  # 1000 / 1.05, received in EUR
    assert inv.payment_state == "paid"
    assert term.reconciled
    full = term.full_reconcile_id
    assert full.exchange_move_id
    fx = full.exchange_move_id
    gain = e.company.income_currency_exchange_account_id
    assert float(fx.line_ids.filtered(lambda r: r.account_id == gain).balance) == pytest.approx(-43.29)
    assert fx.state == "posted"


def test_realised_exchange_loss_in_same_currency(env):
    e = eu_env(env)
    usd = e["core.currency"].search([("name", "=", "USD")])
    vendor = partner(e, "Seattle Supplies")
    bill = invoice(
        e, vendor, [{"price_unit": 1000.0}], move_type="in_invoice", currency_id=usd.ids[0], invoice_date=D(2026, 3, 1)
    )
    usd_bank = e["account.journal"].search([("type", "=", "bank"), ("company_id", "=", e.company.ids[0])], limit=1)
    bill.action_register_payment(journal_id=usd_bank.ids[0], date="2026-03-25", currency_id=usd.ids[0])
    term = bill.line_ids.filtered(lambda r: r.display_type == "payment_term")
    fx = term.full_reconcile_id.exchange_move_id
    loss = e.company.expense_currency_exchange_account_id
    # paying 1000 USD costs 952.38 EUR instead of the 909.09 booked: a loss
    assert float(fx.line_ids.filtered(lambda r: r.account_id == loss).balance) == pytest.approx(43.29)


def test_unrealised_exchange_difference_month_end(env):
    from modules.account.fx import revalue

    e = eu_env(env)
    usd = e["core.currency"].search([("name", "=", "USD")])
    customer = partner(e, "Chicago LLC")
    inv = invoice(e, customer, [{"price_unit": 1200.0}], currency_id=usd.ids[0], invoice_date=D(2026, 3, 1))
    book = float(inv.line_ids.filtered(lambda r: r.display_type == "payment_term").balance)
    assert book == pytest.approx(1090.91)
    move = revalue(e.company, D(2026, 3, 31))  # closing rate 1.20 → 1000.00 EUR
    assert move is not None and move.date == D(2026, 3, 31)
    receivable = e.company.default_receivable_account_id
    adj = move.line_ids.filtered(
        lambda r: r.account_id == receivable and r.partner_id == customer.commercial_partner_id
    )
    assert float(adj.balance) == pytest.approx(-90.91)
    reversal = move.reversal_move_ids
    assert reversal.date == D(2026, 4, 1) and reversal.state == "posted"
    assert adj.reconciled  # matched with its reversal: open items unchanged
    assert inv.amount_residual == pytest.approx(1200.0)
    # running again replaces the pair
    again = revalue(e.company, D(2026, 3, 31))
    assert again != move and move.state == "cancel"


def test_fx_revaluation_job(env, system_env):
    from modules.account.fx import fx_revaluation_job

    e = eu_env(env)
    usd = e["core.currency"].search([("name", "=", "USD")])
    invoice(e, partner(e, "Denver Co"), [{"price_unit": 500.0}], currency_id=usd.ids[0], invoice_date=D(2026, 3, 2))
    result = fx_revaluation_job(system_env, month_end="2026-03-31")
    assert result["month_end"] == "2026-03-31"
    assert result["entries"][e.company.name]
    assert env.ref("account.sched_fx_revaluation").job_name == "account.fx_revaluation"


def test_payment_cannot_be_edited_after_posting(env, customer):
    inv = invoice(env, customer, [{"price_unit": 20.0}])
    result = inv.action_register_payment()
    payment = env["account.payment"].browse(result["payment_ids"])
    with pytest.raises(UserError):
        payment.write({"amount": 30.0})
    with pytest.raises(UserError):
        payment.unlink()
    payment.action_cancel()
    assert payment.state == "cancel"
    assert inv.payment_state == "not_paid"


def test_manual_payment_and_bank_matching_state(env, customer):
    bank = _bank(env)
    bank.write({"payment_through_outstanding": True})
    inv = invoice(env, customer, [{"price_unit": 75.0}])
    inv.action_register_payment(journal_id=bank.ids[0])
    assert inv.payment_state == "in_payment"  # waiting for the bank statement
    payment = env["account.payment"].search([("reconciled_invoice_ids", "in", inv.ids)])
    assert not payment.is_matched
    assert payment.move_id.line_ids.filtered(lambda r: r.account_id == env.company.account_outstanding_receipts_id)
