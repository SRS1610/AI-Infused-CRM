"""ISO 20022 bank-to-customer statements (``camt.053.001.02`` to ``.08``).

Every ``Ntry`` (entry) becomes one statement line, except batched entries: when an entry has several
``TxDtls`` with their own amounts, each transaction becomes a line (their sum is the entry amount; when the
amounts do not add up the entry is kept whole so nothing is lost). References (end-to-end id, account
servicer reference, structured creditor reference), the counterparty (name and IBAN) and the instructed
amount in another currency are kept.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from lxml import etree

from modules.account_bank.importers.base import (
    ParsedStatement,
    ParsedTransaction,
    clean_text,
    safe_xml,
    sanitize_account,
)
from tessera.exceptions import UserError
from tessera.i18n import _

NS_RE = re.compile(r"^urn:iso:std:iso:20022:tech:xsd:camt\.053\.001\.0([2-8])$")
OPENING = ("OPBD", "PRCD", "ITBD")
CLOSING = ("CLBD", "CLAV")


def _xp(path: str) -> str:
    parts = []
    for step in path.split("/"):
        if step in (".", ".."):
            parts.append(step)
        else:
            parts.append(f"*[local-name()='{step}']")
    return "/".join(parts)


def _all(el: etree._Element, path: str) -> list[etree._Element]:
    return el.xpath(_xp(path))  # type: ignore[no-any-return]


def _first(el: etree._Element | None, *paths: str) -> etree._Element | None:
    if el is None:
        return None
    for path in paths:
        found = _all(el, path)
        if found:
            return found[0]
    return None


def _text(el: etree._Element | None, *paths: str) -> str | None:
    node = _first(el, *paths) if paths else el
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _date(el: etree._Element | None) -> dt.date | None:
    text = _text(el, "Dt") or _text(el, "DtTm")
    if not text:
        return None
    return dt.date.fromisoformat(text[:10])


def _signed(amount: Decimal, indicator: str | None) -> Decimal:
    return -amount if (indicator or "").upper() == "DBIT" else amount


def is_camt(content: bytes) -> bool:
    head = content[:4000]
    return b"camt.053.001.0" in head and b"<" in head


def _party(tx: etree._Element, side: str) -> tuple[str | None, str | None]:
    """(name, account) of the debtor or creditor of a transaction (v2-v8 layouts)."""
    parties = _first(tx, "RltdPties")
    if parties is None:
        return None, None
    name = _text(parties, f"{side}/Nm", f"{side}/Pty/Nm")
    account = _text(parties, f"{side}Acct/Id/IBAN", f"{side}Acct/Id/Othr/Id")
    return name, sanitize_account(account)


def _remittance(tx: etree._Element) -> tuple[str, str | None]:
    unstructured = [n.text.strip() for n in _all(tx, "RmtInf/Ustrd") if n.text and n.text.strip()]
    structured = _text(tx, "RmtInf/Strd/CdtrRefInf/Ref")
    label = clean_text(*unstructured)
    return label, structured


def parse(content: bytes) -> list[ParsedStatement]:
    root = safe_xml(content)
    ns = etree.QName(root).namespace or ""
    if not NS_RE.match(ns):
        raise UserError(
            _("Unsupported CAMT document ({namespace}); camt.053.001.02 to .08 are supported", namespace=ns)
        )
    statements: list[ParsedStatement] = []
    for stmt in _all(root, "BkToCstmrStmt/Stmt"):
        account = _text(stmt, "Acct/Id/IBAN", "Acct/Id/Othr/Id")
        st = ParsedStatement(
            name=_text(stmt, "Id") or _text(stmt, "ElctrncSeqNb"),
            currency=_text(stmt, "Acct/Ccy"),
            account_number=sanitize_account(account),
        )
        created = _text(stmt, "CreDtTm")
        for bal in _all(stmt, "Bal"):
            code = _text(bal, "Tp/CdOrPrtry/Cd") or _text(bal, "Tp/CdOrPrtry/Prtry")
            amt_node = _first(bal, "Amt")
            if amt_node is None or not amt_node.text:
                continue
            amount = _signed(Decimal(amt_node.text.strip()), _text(bal, "CdtDbtInd"))
            st.currency = st.currency or amt_node.get("Ccy")
            if code in OPENING and st.balance_start is None:
                st.balance_start = amount
            elif code in CLOSING and st.balance_end is None:
                st.balance_end = amount
                st.date = _date(_first(bal, "Dt"))
        for index, entry in enumerate(_all(stmt, "Ntry")):
            status = _text(entry, "Sts/Cd") or _text(entry, "Sts")
            if status and status.upper() not in ("BOOK", "BOOKED"):
                continue  # pending / informational entries are not part of the booked balance
            st.transactions.extend(_entry_lines(entry, index, st.currency))
        if st.date is None:
            st.date = dt.date.fromisoformat(created[:10]) if created else None
        st.complete_balances()
        statements.append(st)
    if not statements:
        raise UserError(_("The CAMT file contains no statement"))
    return statements


def _entry_lines(entry: etree._Element, index: int, currency: str | None) -> list[ParsedTransaction]:
    amt_node = _first(entry, "Amt")
    if amt_node is None or not amt_node.text:
        raise UserError(_("A CAMT entry has no amount"))
    indicator = _text(entry, "CdtDbtInd")
    amount = _signed(Decimal(amt_node.text.strip()), indicator)
    booking = _date(_first(entry, "BookgDt")) or _date(_first(entry, "ValDt"))
    if booking is None:
        raise UserError(_("A CAMT entry has no booking date"))
    value = _date(_first(entry, "ValDt"))
    entry_ref = _text(entry, "AcctSvcrRef") or _text(entry, "NtryRef")
    entry_info = _text(entry, "AddtlNtryInf")
    family = clean_text(
        _text(entry, "BkTxCd/Domn/Cd"),
        _text(entry, "BkTxCd/Domn/Fmly/Cd"),
        _text(entry, "BkTxCd/Domn/Fmly/SubFmlyCd"),
        sep="/",
    ) or _text(entry, "BkTxCd/Prtry/Cd")
    details = _all(entry, "NtryDtls/TxDtls")
    amounts: list[Decimal | None] = []
    for tx in details:
        node = _first(tx, "Amt", "AmtDtls/TxAmt/Amt")
        tx_ind = _text(tx, "CdtDbtInd") or indicator
        amounts.append(_signed(Decimal(node.text.strip()), tx_ind) if node is not None and node.text else None)
    split = len(details) > 1 and all(a is not None for a in amounts) and sum(a for a in amounts if a) == amount
    groups = list(zip(details, amounts, strict=True)) if split else [(details[0] if details else None, amount)]
    lines: list[ParsedTransaction] = []
    for sub, (tx, tx_amount) in enumerate(groups):
        assert tx_amount is not None
        incoming = tx_amount > 0
        partner_name = account = None
        label, structured = ("", None)
        refs: dict[str, str | None] = {}
        foreign_cur = foreign_amt = None
        tx_info = None
        if tx is not None:
            partner_name, account = _party(tx, "Dbtr" if incoming else "Cdtr")
            if not partner_name:
                partner_name, account = _party(tx, "UltmtDbtr" if incoming else "UltmtCdtr")
            label, structured = _remittance(tx)
            refs = {
                "end_to_end": _text(tx, "Refs/EndToEndId"),
                "servicer": _text(tx, "Refs/AcctSvcrRef"),
                "tx_id": _text(tx, "Refs/TxId"),
                "instr": _text(tx, "Refs/InstrId"),
            }
            if refs["end_to_end"] == "NOTPROVIDED":
                refs["end_to_end"] = None
            instructed = _first(tx, "AmtDtls/InstdAmt/Amt")
            if (
                instructed is not None
                and instructed.text
                and instructed.get("Ccy")
                and instructed.get("Ccy") != currency
            ):
                foreign_cur = instructed.get("Ccy")
                foreign_amt = Decimal(instructed.text.strip()) * (1 if incoming else -1)
            tx_info = _text(tx, "AddtlTxInf")
        payment_ref = clean_text(label, structured) or clean_text(tx_info, entry_info) or entry_ref or ""
        bank_id = refs.get("servicer") or entry_ref
        unique = f"{bank_id or ''}-{refs.get('end_to_end') or refs.get('tx_id') or ''}-{index}-{sub}"
        lines.append(
            ParsedTransaction(
                date=booking,
                value_date=value,
                amount=tx_amount,
                payment_ref=payment_ref,
                partner_name=partner_name,
                account_number=account,
                ref=structured or refs.get("end_to_end") or bank_id,
                unique_import_id=unique,
                transaction_type=family,
                foreign_currency=foreign_cur,
                amount_currency=foreign_amt,
                narration=clean_text(entry_info, tx_info, sep="\n") or None,
            )
        )
    return lines
