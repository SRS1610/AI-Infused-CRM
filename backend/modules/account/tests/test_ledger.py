"""Ledger core: invoices, numbering (2-b), locks, immutability and the database guard (D-073)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError

from modules.account.tests.common import D, invoice, lines_by_type, make_tax, partner
from tessera.exceptions import UserError, ValidationError

pytestmark = pytest.mark.db


@pytest.fixture
def tax10(env):
    return make_tax(env, "Test tax 10%", 10)


@pytest.fixture
def customer(env):
    return partner(env)


def test_invoice_builds_a_balanced_double_entry(env, tax10, customer):
    move = invoice(
        env,
        customer,
        [
            {"name": "Consulting", "quantity": 2, "price_unit": 150.0, "tax_ids": [("set", tax10.ids)]},
            {"name": "Licence", "quantity": 1, "price_unit": 99.99, "discount": 10, "tax_ids": [("set", tax10.ids)]},
        ],
        post=False,
    )
    assert move.name == "/"
    assert move.amount_untaxed == pytest.approx(389.99)
    assert move.amount_tax == pytest.approx(39.0)
    assert move.amount_total == pytest.approx(428.99)
    kinds = lines_by_type(move)
    assert sorted(b for b, _c in kinds["product"]) == [-300.0, -89.99]
    assert kinds["tax"] == [(-39.0, -39.0)]
    assert kinds["payment_term"] == [(428.99, 428.99)]
    assert sum(float(line.balance) for line in move.line_ids) == pytest.approx(0)
    receivable = move.line_ids.filtered(lambda line: line.display_type == "payment_term")
    assert receivable.account_id.account_type == "asset_receivable"
    assert receivable.partner_id == customer


def test_editing_a_draft_resyncs_taxes_and_totals(env, tax10, customer):
    move = invoice(env, customer, [{"quantity": 1, "price_unit": 100.0, "tax_ids": [("set", tax10.ids)]}], post=False)
    line = move.invoice_line_ids[0]
    move.write({"invoice_line_ids": [("update", line.ids[0], {"price_unit": 200.0})]})
    assert move.amount_total == pytest.approx(220.0)
    move.write({"invoice_line_ids": [("create", {"name": "Extra", "quantity": 1, "price_unit": 50.0})]})
    assert move.amount_untaxed == pytest.approx(250.0)
    assert move.amount_tax == pytest.approx(20.0)
    assert len(move.line_ids.filtered(lambda r: r.display_type == "tax")) == 1


def test_vendor_bill_signs(env, tax10):
    vendor = partner(env, "Supplies Ltd")
    tax_in = make_tax(env, "Test purchase 10%", 10, use="purchase")
    bill = invoice(
        env, vendor, [{"quantity": 3, "price_unit": 10.0, "tax_ids": [("set", tax_in.ids)]}], move_type="in_invoice"
    )
    kinds = lines_by_type(bill)
    assert kinds["product"] == [(30.0, 30.0)]
    assert kinds["tax"] == [(3.0, 3.0)]
    assert kinds["payment_term"] == [(-33.0, -33.0)]
    assert (
        bill.line_ids.filtered(lambda r: r.display_type == "payment_term").account_id.account_type
        == "liability_payable"
    )
    assert bill.amount_total_signed == pytest.approx(-33.0)
    assert bill.name.startswith("BILL/2026/")


def test_numbering_is_gapless_per_journal_year_and_refund(env, tax10, customer):
    first = invoice(env, customer, [{"price_unit": 10.0}])
    second = invoice(env, customer, [{"price_unit": 20.0}])
    prefix, n1 = first.name.rsplit("/", 1)
    assert prefix == "INV/2026"
    assert second.name == f"INV/2026/{int(n1) + 1:05d}"
    draft = invoice(env, customer, [{"price_unit": 5.0}], post=False)
    draft.unlink()  # abandoned drafts never consume a number
    third = invoice(env, customer, [{"price_unit": 30.0}])
    assert third.name == f"INV/2026/{int(n1) + 2:05d}"
    next_year = invoice(env, customer, [{"price_unit": 30.0}], invoice_date=D(2027, 1, 5))
    assert next_year.name.startswith("INV/2027/")
    refund = invoice(env, customer, [{"price_unit": 5.0}], move_type="out_refund")
    assert refund.name.startswith("RINV/2026/")


def test_posted_entries_keep_their_number_and_cannot_be_deleted(env, customer):
    move = invoice(env, customer, [{"price_unit": 10.0}])
    name = move.name
    with pytest.raises(UserError):
        move.unlink()
    move.button_cancel()
    assert move.state == "cancel" and move.name == name
    with pytest.raises(UserError):
        move.unlink()


def test_posted_entries_are_read_only(env, customer):
    sales = env["account.journal"].search([("code", "=", "INV"), ("company_id", "=", env.company.ids[0])])
    sales.write({"restrict_mode_hash_table": False})
    move = invoice(env, customer, [{"price_unit": 10.0}])
    with pytest.raises(UserError):
        move.write({"partner_id": partner(env, "Other").ids[0]})
    with pytest.raises(UserError):
        move.line_ids[0].write({"balance": 5.0})
    with pytest.raises(UserError):
        move.write({"state": "draft"})
    move.write({"ref": "PO-42"})  # narrative fields stay editable
    assert move.ref == "PO-42"
    move.button_draft()
    move.write({"invoice_line_ids": [("update", move.invoice_line_ids[0].ids[0], {"price_unit": 12.0})]})
    move.action_post()
    assert move.amount_total == pytest.approx(12.0)


def test_lock_dates_refuse_posting_and_cancelling(env, customer):
    company = env.company
    posted = invoice(env, customer, [{"price_unit": 10.0}], invoice_date=D(2026, 1, 15))
    company.write({"fiscalyear_lock_date": D(2026, 1, 31)})
    with pytest.raises(UserError) as exc:
        posted.button_cancel()
    assert "locked" in str(exc.value)
    with pytest.raises(UserError):
        posted.button_draft()
    draft = invoice(env, customer, [{"price_unit": 10.0}], invoice_date=D(2026, 1, 20), post=False)
    with pytest.raises(UserError):
        draft.action_post()


def test_period_lock_binds_non_advisers_only(env, env_for, customer):
    env.company.write({"period_lock_date": D(2026, 2, 28)})
    move = invoice(env, customer, [{"price_unit": 10.0}], invoice_date=D(2026, 2, 10), post=False)
    move.action_post()  # admin is an adviser
    assert move.state == "posted"
    user = env["core.user"].create(
        {
            "name": "Billing clerk",
            "login": "clerk@test",
            "group_ids": [("set", [env.ref("account.group_account_invoice").ids[0]])],
        }
    )
    clerk_env = env.with_user(user.ids[0])
    other = invoice(
        clerk_env, customer.with_env(clerk_env), [{"price_unit": 10.0}], invoice_date=D(2026, 2, 11), post=False
    )
    with pytest.raises(UserError):
        other.action_post()
    with pytest.raises(UserError):
        clerk_env.company.write({"period_lock_date": D(2026, 1, 1)})


def test_unbalanced_entries_are_refused(env):
    journal = env["account.journal"].search([("code", "=", "MISC"), ("company_id", "=", env.company.ids[0])])
    acc_a, acc_b = env["account.account"].search([("company_id", "=", env.company.ids[0])], limit=2)
    move = env["account.move"].create(
        {
            "journal_id": journal.ids[0],
            "line_ids": [
                ("create", {"account_id": acc_a.ids[0], "debit": 100.0, "name": "a"}),
                ("create", {"account_id": acc_b.ids[0], "credit": 90.0, "name": "b"}),
            ],
        }
    )
    with pytest.raises(UserError):
        move.action_post()


def test_database_guard_rejects_unbalanced_posted_entries(env, cr, customer):
    """Even a direct SQL write cannot leave a posted entry unbalanced (deferred constraint trigger)."""
    move = invoice(env, customer, [{"price_unit": 10.0}])
    line = move.line_ids.filtered(lambda r: r.display_type == "product")
    cr.execute("SAVEPOINT guard")
    cr.execute("UPDATE account_move_line SET credit = 15, balance = -15 WHERE id = :id", {"id": line.ids[0]})
    with pytest.raises(DBAPIError) as exc:
        cr.execute("SET CONSTRAINTS ALL IMMEDIATE")
    assert "not balanced" in str(exc.value)
    cr.execute("ROLLBACK TO SAVEPOINT guard")


def test_check_constraints_on_items(env, cr, customer):
    move = invoice(env, customer, [{"price_unit": 10.0}])
    line = move.line_ids[0]
    cr.execute("SAVEPOINT chk")
    with pytest.raises(DBAPIError):
        cr.execute(
            "UPDATE account_move_line SET debit = 5, credit = 5, balance = 0 WHERE id = :id", {"id": line.ids[0]}
        )
    cr.execute("ROLLBACK TO SAVEPOINT chk")


def test_hash_chain_on_sales_journal(env, customer):
    from modules.account.integrity import check_integrity

    a = invoice(env, customer, [{"price_unit": 10.0}])
    b = invoice(env, customer, [{"price_unit": 20.0}])
    assert a.inalterable_hash and b.inalterable_hash
    assert b.secure_sequence_number == a.secure_sequence_number + 1
    with pytest.raises(UserError):
        a.button_draft()
    report = check_integrity(env, a.journal_id)
    assert report["ok"] and report["checked"] >= 2
    # tamper with a posted amount behind the ORM's back (keeping the entry balanced)
    env.cr.execute("UPDATE account_move SET ref = ref WHERE id = :id", {"id": a.ids[0]})
    env.cr.execute(
        "UPDATE account_move_line SET debit = debit + 1, balance = balance + 1 WHERE move_id = :id AND display_type = 'payment_term'",
        {"id": a.ids[0]},
    )
    env.cr.execute(
        "UPDATE account_move_line SET credit = credit + 1, balance = balance - 1 WHERE move_id = :id AND display_type = 'product'",
        {"id": a.ids[0]},
    )
    env.invalidate_all()
    report = check_integrity(env, a.journal_id)
    assert not report["ok"]
    assert report["first_broken"] == a.name


def test_receivable_accounts_always_reconcile_and_used_accounts_stay(env, customer):
    acc = env["account.account"].create(
        {"code": "1199", "name": "Other receivable", "account_type": "asset_receivable"}
    )
    assert acc.reconcile
    with pytest.raises(ValidationError):
        acc.write({"reconcile": False})
    move = invoice(env, customer, [{"price_unit": 10.0}])
    used = move.line_ids[0].account_id
    with pytest.raises(ValidationError):
        used.unlink()


def test_fiscal_year_numbering_and_close(env, customer):
    fy = env["account.fiscal_year"].create(
        {"name": "FY 2026/27", "date_from": D(2026, 7, 1), "date_to": D(2027, 6, 30)}
    )
    move = invoice(env, customer, [{"price_unit": 10.0}], invoice_date=D(2026, 8, 1))
    assert move.name.startswith("INV/2027/")
    with pytest.raises(ValidationError):
        env["account.fiscal_year"].create({"name": "Overlap", "date_from": D(2027, 1, 1), "date_to": D(2027, 12, 31)})
    draft = invoice(env, customer, [{"price_unit": 10.0}], invoice_date=D(2026, 9, 1), post=False)
    with pytest.raises(UserError):
        fy.action_close()
    draft.unlink()
    fy.action_close()
    assert fy.state == "closed"
    assert env.company.fiscalyear_lock_date == D(2027, 6, 30)


def test_negative_invoice_refused(env, customer):
    move = invoice(env, customer, [{"price_unit": -10.0}], post=False)
    with pytest.raises(UserError):
        move.action_post()


def test_invoice_requires_partner_and_lines(env, customer):
    move = env["account.move"].create({"move_type": "out_invoice"})
    with pytest.raises(UserError):
        move.action_post()
    move.write({"partner_id": customer.ids[0]})
    with pytest.raises(UserError):
        move.action_post()
