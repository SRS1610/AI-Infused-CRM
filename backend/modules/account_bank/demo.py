"""Bank demo scenario (``tessera seed demo --phase 2``), run after the accounting demo.

For every company with a chart: the shipped reconciliation rules, the main bank journal switched to
*Payments wait for bank matching* with two customer payments registered (invoices *In payment*), and a
CAMT.053 statement built from the company's open demo documents — customer payments quoting the invoice
numbers, a batched transfer for the two registered payments, vendor bill payments, bank fees, interest and
one unknown transfer — imported and auto-reconciled (2-d: at least 90 % of its lines are matched by the
shipped rules; the unknown transfer is left for review).
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from xml.sax.saxutils import escape

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
DEMO_STATEMENT = "DEMO-CAMT"
DEMO_IBAN = {"USD": "US64SVBKUS6S3300958879", "EUR": "FR7630006000011234567890189"}


def _entry(
    amount: float, currency: str, day: dt.date, ref: str, txs: list[dict[str, Any]], info: str | None = None
) -> str:
    indicator = "CRDT" if amount > 0 else "DBIT"
    details = ""
    for tx in txs:
        side = "Dbtr" if tx["amount"] > 0 else "Cdtr"
        party = (
            f"<RltdPties><{side}><Pty><Nm>{escape(tx['name'])}</Nm></Pty></{side}></RltdPties>"
            if tx.get("name")
            else ""
        )
        remittance = f"<RmtInf><Ustrd>{escape(tx['label'])}</Ustrd></RmtInf>" if tx.get("label") else ""
        details += (
            f"<TxDtls><Refs><EndToEndId>{escape(tx['e2e'])}</EndToEndId></Refs>"
            f'<Amt Ccy="{currency}">{abs(tx["amount"]):.2f}</Amt><CdtDbtInd>{indicator}</CdtDbtInd>'
            f"{party}{remittance}</TxDtls>"
        )
    batch = f"<Btch><NbOfTxs>{len(txs)}</NbOfTxs></Btch>" if len(txs) > 1 else ""
    return (
        f'<Ntry><Amt Ccy="{currency}">{abs(amount):.2f}</Amt><CdtDbtInd>{indicator}</CdtDbtInd>'
        f"<Sts><Cd>BOOK</Cd></Sts><BookgDt><Dt>{day.isoformat()}</Dt></BookgDt><ValDt><Dt>{day.isoformat()}</Dt></ValDt>"
        f"<AcctSvcrRef>{escape(ref)}</AcctSvcrRef>"
        + (f"<AddtlNtryInf>{escape(info)}</AddtlNtryInf>" if info else "")
        + (f"<NtryDtls>{batch}{details}</NtryDtls>" if txs else "")
        + "</Ntry>"
    )


def build_demo_camt(env: Any, company: Any, *, register_payments: bool = True) -> bytes:
    """A CAMT.053 (v08) statement paying the company's open demo documents (see the module docstring)."""
    env = env.with_company(company)
    company = company.elevated()
    currency = company.currency_id.name
    today = env.today()
    moves = env["account.move"].elevated()
    open_docs = moves.search(
        [
            ("company_id", "=", company.ids[0]),
            ("state", "=", "posted"),
            ("ref", "like", "DEMO-"),
            ("move_type", "in", ["out_invoice", "in_invoice"]),
            ("payment_state", "in", ["not_paid", "partial"]),
        ],
        order="invoice_date, id",
    )
    refunded = moves.search([("company_id", "=", company.ids[0]), ("ref", "=", "DEMO-CN")]).mapped("reversed_entry_id")
    open_docs = open_docs - refunded  # invoice and credit note cancel each other out
    invoices = open_docs.filtered(lambda m: m.move_type == "out_invoice")
    bills = open_docs.filtered(lambda m: m.move_type == "in_invoice")
    entries: list[tuple[float, str]] = []
    n = 0

    def ref() -> str:
        nonlocal n
        n += 1
        return f"{DEMO_STATEMENT}-{company.ids[0]}-{n:03d}"

    paid_by_payment = invoices[-2:] if register_payments and len(invoices) > 2 else invoices.browse([])
    if paid_by_payment:
        bank = (
            env["account.journal"]
            .elevated()
            .search([("company_id", "=", company.ids[0]), ("type", "=", "bank")], order="sequence, id", limit=1)
        )
        bank.write({"payment_through_outstanding": True})
        txs = []
        for inv in paid_by_payment:
            amount = float(inv.amount_residual)
            inv.action_register_payment(journal_id=bank.ids[0], date=today.isoformat())
            txs.append({"amount": amount, "name": inv.partner_id.name, "label": inv.name, "e2e": f"E2E-{inv.name}"})
        total = round(sum(t["amount"] for t in txs), 2)
        entries.append((total, _entry(total, currency, today, ref(), txs, info="Batch credit transfer")))
    for inv in invoices - paid_by_payment:
        amount = float(inv.amount_residual)
        tx = {"amount": amount, "name": inv.partner_id.name, "label": f"Payment {inv.name}", "e2e": f"E2E-{inv.name}"}
        entries.append((amount, _entry(amount, currency, today, ref(), [tx])))
    for bill in bills:
        amount = -float(bill.amount_residual)
        tx = {"amount": amount, "name": bill.partner_id.name, "label": bill.ref, "e2e": f"OUT-{bill.ref}"}
        entries.append((amount, _entry(amount, currency, today, ref(), [tx])))
    for label, fee in (("Bank fees October", -18.5), ("Account maintenance fee", -9.0), ("Card fees", -4.75)):
        entries.append((fee, _entry(fee, currency, today, ref(), [], info=label)))
    entries.append((12.34, _entry(12.34, currency, today, ref(), [], info="Interest Q3")))
    unknown = {"amount": 137.42, "name": "J. Smith", "label": "Transfer", "e2e": "NOTPROVIDED"}
    entries.append((137.42, _entry(137.42, currency, today, ref(), [unknown])))
    opening = 25000.0
    closing = round(opening + sum(amount for amount, _xml in entries), 2)
    iban = DEMO_IBAN.get(currency, DEMO_IBAN["EUR"])
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.08"><BkToCstmrStmt>'
        f"<GrpHdr><MsgId>{DEMO_STATEMENT}-{company.ids[0]}</MsgId><CreDtTm>{today.isoformat()}T06:00:00</CreDtTm></GrpHdr>"
        f"<Stmt><Id>{DEMO_STATEMENT}-{company.ids[0]}-{today.isoformat()}</Id><CreDtTm>{today.isoformat()}T06:00:00</CreDtTm>"
        f"<Acct><Id><IBAN>{iban}</IBAN></Id><Ccy>{currency}</Ccy></Acct>"
        f'<Bal><Tp><CdOrPrtry><Cd>OPBD</Cd></CdOrPrtry></Tp><Amt Ccy="{currency}">{opening:.2f}</Amt>'
        f"<CdtDbtInd>CRDT</CdtDbtInd><Dt><Dt>{(today - dt.timedelta(days=1)).isoformat()}</Dt></Dt></Bal>"
        f'<Bal><Tp><CdOrPrtry><Cd>CLBD</Cd></CdOrPrtry></Tp><Amt Ccy="{currency}">{abs(closing):.2f}</Amt>'
        f"<CdtDbtInd>{'CRDT' if closing >= 0 else 'DBIT'}</CdtDbtInd><Dt><Dt>{today.isoformat()}</Dt></Dt></Bal>"
        + "".join(xml for _amount, xml in entries)
        + "</Stmt></BkToCstmrStmt></Document>"
    )
    return xml.encode("utf-8")


def seed_company(env: Any, company: Any) -> dict[str, Any] | None:
    from modules.account_bank.importing import import_statements
    from modules.account_bank.rules import install_default_models

    env = env.with_company(company)
    install_default_models(company)
    if (
        env["account.bank_statement"]
        .elevated()
        .search_count([("company_id", "=", company.ids[0]), ("name", "like", f"{DEMO_STATEMENT}%")])
    ):
        return None
    journal = (
        env["account.journal"]
        .elevated()
        .search([("company_id", "=", company.ids[0]), ("type", "=", "bank")], order="sequence, id", limit=1)
    )
    if not journal:
        return None
    content = build_demo_camt(env, company)
    attachment = (
        env["core.attachment"]
        .elevated()
        .create(
            {"name": "demo_camt053.xml", "raw": content, "mimetype": "application/xml", "company_id": company.ids[0]}
        )
    )
    return import_statements(journal, content, "demo_camt053.xml", attachment=attachment)


@hook("seed_demo")
def seed_bank(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        result = seed_company(env, company)
        if result:
            log.info("bank demo", company=company.name, lines=result["lines"], reconciled=result["reconciled"])
