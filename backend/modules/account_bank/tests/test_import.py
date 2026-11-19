"""Statement import (2-d): every sample format imports without loss, duplicates are skipped."""

from __future__ import annotations

from decimal import Decimal

import pytest

from modules.account.tests.common import eu_env
from modules.account_bank.importers import detect_format, parse_file
from modules.account_bank.importers.mt940 import parse_86
from modules.account_bank.importing import import_statements
from modules.account_bank.tests.common import bank_journal, sample
from tessera.exceptions import UserError

# name, format, statements, lines, total, opening, closing (first statement), checks
EXPECTED = {
    "sample_camt053_v02.xml": ("camt053", 1, 5, "3654.50", "10000.00", "13654.50"),
    "sample_camt053_v08.xml": ("camt053", 1, 2, "-230.00", "100.00", "-130.00"),
    "sample_ofx_v1.ofx": ("ofx", 1, 4, "1147.51", "5000.00", "6147.51"),
    "sample_ofx_v2.ofx": ("ofx", 1, 3, "1335.20", "6147.51", "7482.71"),
    "sample_mt940.sta": ("mt940", 2, 5, "3674.50", "10000.00", "14154.50"),
    "sample_bank.csv": ("csv", 1, 5, "1057.52", "5000.00", "6057.52"),
    "sample_bank_debit_credit.csv": ("csv", 1, 4, "3654.50", "0", "3654.50"),
}


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_parsers_lose_nothing(name):
    kind, statements = parse_file(sample(name), name)
    fmt, count, lines, total, opening, closing = EXPECTED[name]
    assert kind == fmt == detect_format(sample(name), name)
    assert len(statements) == count
    transactions = [t for st in statements for t in st.transactions]
    assert len(transactions) == lines
    assert sum((t.amount for t in transactions), Decimal(0)) == Decimal(total)
    assert statements[0].balance_start == Decimal(opening)
    assert statements[0].balance_end == Decimal(closing)
    for st in statements:
        assert st.balance_start + st.total == st.balance_end
        assert all(t.date and t.payment_ref for t in st.transactions)


def test_camt_keeps_references_parties_and_currency():
    _kind, (st,) = parse_file(sample("sample_camt053_v02.xml"))
    lyon, fees, batch1, batch2, supplies = st.transactions
    assert st.account_number == "FR7630006000011234567890189" and st.currency == "EUR"
    assert lyon.partner_name == "Lyon Robotics SA" and lyon.account_number == "FR1420041010050500013M02606"
    assert lyon.ref == "E2E-LYON-0001" and "INV/2026/00012" in lyon.payment_ref
    assert fees.amount == Decimal("-45.50") and fees.payment_ref == "Bank fees March 2026"
    assert (batch1.amount, batch2.amount) == (Decimal("1800.00"), Decimal("1200.00"))  # batch split per transaction
    assert batch1.ref == "RF18539007547034" and batch2.partner_name == "Brussels Components NV"
    assert supplies.foreign_currency == "USD" and supplies.amount_currency == Decimal("-540.00")
    assert supplies.partner_name == "Office Supplies GmbH"  # creditor of an outgoing payment
    assert len({t.unique_import_id for t in st.transactions}) == 5


def test_camt_v08_layout_and_negative_balance():
    _kind, (st,) = parse_file(sample("sample_camt053_v08.xml"))
    rent, refund = st.transactions
    assert rent.partner_name == "Paris Office Leasing" and rent.amount == Decimal("-250.00")
    assert refund.ref == "V08-0002" and refund.payment_ref == "Refund of deposit"
    assert st.balance_end == Decimal("-130.00")


def test_mt940_details():
    _kind, (first, second) = parse_file(sample("sample_mt940.sta"))
    lyon, _fees, batch = first.transactions
    assert lyon.partner_name == "Lyon Robotics SA" and lyon.ref == "E2E-LYON-0001"
    assert lyon.payment_ref == "Payment invoice INV/2026/00012"
    assert batch.partner_name == "Rotterdam Logistics BV" and batch.account_number == "NL91ABNA0417164300"
    assert "INV/2026/00015" in batch.payment_ref
    bill, reversal = second.transactions
    assert bill.partner_name == "Office Supplies GmbH" and bill.ref == "BILL-4711"
    assert reversal.amount == Decimal("20.00")  # RD = reversal of a debit
    assert parse_86("free text only") == {"name": None, "account": None, "label": "free text only", "ref": None}


def test_ofx_details():
    _kind, (st,) = parse_file(sample("sample_ofx_v1.ofx"))
    assert st.account_number == "4000123456" and st.currency == "USD"
    assert st.transactions[1].partner_name == "CLOUD HOSTING & CO"
    assert st.transactions[2].ref == "1042"
    _kind, (st2,) = parse_file(sample("sample_ofx_v2.ofx"))
    assert st2.transactions[0].partner_name == "Globex Corporation" and st2.transactions[0].ref == "DEP-77812"
    assert st2.transactions[2].foreign_currency == "EUR" and st2.transactions[2].amount_currency == Decimal("-60.00")


def test_csv_options_and_errors():
    content = b"when|what|value\n03/15/2026|Coffee|-3,50\n03/16/2026|Refund|1.000,00\n"
    options = {
        "delimiter": "|",
        "columns": {"date": "when", "payment_ref": "what", "amount": 2},
        "date_format": "%m/%d/%Y",
        "decimal_separator": ",",
        "thousands_separator": ".",
        "balance_start": "10",
    }
    _kind, (st,) = parse_file(content, "x.csv", options=options)
    assert [t.amount for t in st.transactions] == [Decimal("-3.50"), Decimal("1000.00")]
    assert st.balance_end == Decimal("1006.50")
    with pytest.raises(UserError):
        parse_file(b"a,b\n1,2\n", "x.csv")
    with pytest.raises(UserError):
        parse_file(b"", "x.csv")
    with pytest.raises(UserError):
        parse_file(
            b"<Document xmlns='urn:iso:std:iso:20022:tech:xsd:camt.052.001.02'/>", "x.xml", file_format="camt053"
        )
    with pytest.raises(UserError):
        parse_file(sample("sample_bank.csv").replace(b"6057.52", b"6000.00"), "bad.csv")  # balances do not add up


@pytest.mark.db
@pytest.mark.parametrize(
    "name,company",
    [
        ("sample_camt053_v02.xml", "eu"),
        ("sample_camt053_v08.xml", "eu"),
        ("sample_mt940.sta", "eu"),
        ("sample_bank_debit_credit.csv", "eu"),
        ("sample_ofx_v1.ofx", "us"),
        ("sample_ofx_v2.ofx", "us"),
        ("sample_bank.csv", "us"),
    ],
)
def test_import_into_journal_twice(env, name, company):
    e = eu_env(env) if company == "eu" else env
    journal = bank_journal(e)
    fmt, count, lines, total, _opening, _closing = EXPECTED[name]
    first = import_statements(journal, sample(name), name, auto_reconcile=False)
    assert first["format"] == fmt and first["lines"] == lines and first["duplicates"] == 0
    statements = e["account.bank_statement"].browse(first["statement_ids"])
    assert len(statements) == count
    st_lines = statements.mapped("line_ids")
    assert sum(float(x.amount) for x in st_lines) == pytest.approx(float(total))
    assert all(x.state == "posted" and x.move_id.state == "posted" for x in st_lines)
    assert all(st.difference == pytest.approx(0) for st in statements)
    again = import_statements(journal, sample(name), name)
    assert again["lines"] == 0 and again["duplicates"] == lines and again["statement_ids"] == []


@pytest.mark.db
def test_import_checks_journal_currency_and_account(env):
    journal = bank_journal(env)  # USD company
    with pytest.raises(UserError):
        import_statements(journal, sample("sample_camt053_v02.xml"))
    e = eu_env(env)
    eu_journal = bank_journal(e)
    holder = e["contacts.partner"].create({"name": "EU Holder"})
    account = e["account.bank_account"].create(
        {"partner_id": holder.ids[0], "acc_number": "DE89 3704 0044 0532 0130 00"}
    )
    eu_journal.write({"bank_account_id": account.ids[0]})
    with pytest.raises(UserError):
        import_statements(eu_journal, sample("sample_camt053_v02.xml"))
    with pytest.raises(UserError):
        import_statements(
            e["account.journal"].search([("type", "=", "sale"), ("company_id", "=", e.company.ids[0])], limit=1), b"x"
        )


@pytest.mark.db
def test_partial_reimport_and_csv_continues_balance(env):
    e = eu_env(env)
    journal = bank_journal(e)
    content = sample("sample_bank_debit_credit.csv")
    head = b"\n".join(content.splitlines()[:3]) + b"\n"
    first = import_statements(journal, head, "part.csv", auto_reconcile=False)
    assert first["lines"] == 2
    st1 = e["account.bank_statement"].browse(first["statement_ids"])
    rest = import_statements(journal, content, "full.csv", auto_reconcile=False)
    assert rest["lines"] == 2 and rest["duplicates"] == 2
    st2 = e["account.bank_statement"].browse(rest["statement_ids"])
    assert st2.balance_start == pytest.approx(float(st1.balance_end_real))


@pytest.mark.db
def test_partner_found_from_iban_and_name(env):
    e = eu_env(env)
    lyon = e["contacts.partner"].create({"name": "Lyon Robotics SA", "is_company": True})
    e["account.bank_account"].create({"partner_id": lyon.ids[0], "acc_number": "FR1420041010050500013M02606"})
    brussels = e["contacts.partner"].create({"name": "Brussels Components NV", "is_company": True})
    result = import_statements(bank_journal(e), sample("sample_camt053_v02.xml"), auto_reconcile=False)
    lines = e["account.bank_statement"].browse(result["statement_ids"]).line_ids
    by_name = {x.partner_name: x.partner_id for x in lines}
    assert by_name["Lyon Robotics SA"] == lyon
    assert by_name["Brussels Components NV"] == brussels


@pytest.mark.db
def test_import_record(env):
    e = eu_env(env)
    journal = bank_journal(e)
    rec = e["account.bank_statement_import"].create(
        {"journal_id": journal.ids[0], "data_file": sample("sample_mt940.sta"), "filename": "march.sta"}
    )
    result = rec.action_import()
    assert rec.state == "done" and rec.file_format == "mt940" and rec.line_count == 5
    assert rec.attachment_id and not rec.data_file and len(rec.statement_ids) == 2
    with pytest.raises(UserError):
        rec.action_import()
    assert result["lines"] == 5
