"""Open Financial Exchange statements: OFX 1.x (SGML, unclosed leaf tags) and OFX 2.x (XML).

Bank (``STMTRS``) and credit-card (``CCSTMTRS``) statements are read; ``FITID`` is the unique id. OFX has no
opening balance: it is derived from the ledger balance (``LEDGERBAL``) minus the transactions.
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
    to_decimal,
)
from tessera.exceptions import UserError
from tessera.i18n import _

TAG_RE = re.compile(r"<(/?)([A-Za-z0-9.]+)>([^<]*)")


def is_ofx(content: bytes) -> bool:
    head = content[:2000].upper()
    return b"OFXHEADER" in head or b"<OFX>" in head


def _sgml_to_xml(text: str) -> str:
    """Close the leaf elements of an OFX 1.x body (``<NAME>value`` → ``<NAME>value</NAME>``)."""
    body = text[text.upper().index("<OFX>") :]
    out: list[str] = []
    for match in TAG_RE.finditer(body):
        closing, tag, value = match.group(1), match.group(2).upper(), match.group(3)
        value = value.strip()
        if closing:
            out.append(f"</{tag}>")
        elif value:
            esc = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            esc = re.sub(r"&amp;(amp|lt|gt|quot|apos);", r"&\1;", esc)
            out.append(f"<{tag}>{esc}</{tag}>")
        else:
            out.append(f"<{tag}>")
    xml = "".join(out)
    # leaf tags that were closed explicitly in the source are now closed twice: drop the extra closers
    for _round in range(3):
        xml = re.sub(r"(<([A-Z0-9.]+)>[^<]*</\2>)</\2>", r"\1", xml)
    return xml


def _decode(content: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("latin-1", errors="replace")


def _date(text: str | None) -> dt.date | None:
    if not text:
        return None
    digits = text.strip()[:8]
    return dt.datetime.strptime(digits, "%Y%m%d").date()


def _t(el: etree._Element | None, tag: str) -> str | None:
    if el is None:
        return None
    node = el.find(f".//{tag}")
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def parse(content: bytes) -> list[ParsedStatement]:
    text = _decode(content)
    stripped = text.lstrip()
    if stripped.startswith("<?xml") or "<?OFX" in text[:500]:
        root = safe_xml(text[text.index("<") :].encode("utf-8"))
    else:
        if "<OFX>" not in text.upper():
            raise UserError(_("The file is not an OFX statement"))
        root = safe_xml(_sgml_to_xml(text).encode("utf-8"))
    statements: list[ParsedStatement] = []
    for stmtrs in [*root.iter("STMTRS"), *root.iter("CCSTMTRS")]:
        acct = stmtrs.find("BANKACCTFROM")
        if acct is None:
            acct = stmtrs.find("CCACCTFROM")
        st = ParsedStatement(
            currency=_t(stmtrs, "CURDEF"),
            account_number=sanitize_account(_t(acct, "ACCTID")),
        )
        tranlist = stmtrs.find("BANKTRANLIST")
        for trn in tranlist.iter("STMTTRN") if tranlist is not None else []:
            posted = _date(_t(trn, "DTPOSTED")) or _date(_t(trn, "DTUSER"))
            if posted is None:
                raise UserError(_("An OFX transaction has no date"))
            name = _t(trn, "NAME")  # plain NAME or PAYEE/NAME
            memo = _t(trn, "MEMO")
            currency_node = trn.find("CURRENCY")
            if currency_node is None:
                currency_node = trn.find("ORIGCURRENCY")
            amount = to_decimal(_t(trn, "TRNAMT"))
            foreign = amount_cur = None
            if currency_node is not None and _t(currency_node, "CURSYM") and _t(currency_node, "CURRATE"):
                foreign = _t(currency_node, "CURSYM")
                rate = Decimal(_t(currency_node, "CURRATE") or "1")
                # CURRATE is the ratio of the statement currency to CURSYM: CURSYM amount = amount / rate
                amount_cur = (amount / rate).quantize(Decimal("0.01")) if rate else None
            st.transactions.append(
                ParsedTransaction(
                    date=posted,
                    amount=amount,
                    payment_ref=clean_text(memo) or clean_text(name) or "",
                    partner_name=name,
                    account_number=sanitize_account(_t(trn.find("BANKACCTTO"), "ACCTID")),
                    ref=_t(trn, "REFNUM") or _t(trn, "CHECKNUM"),
                    unique_import_id=_t(trn, "FITID"),
                    transaction_type=_t(trn, "TRNTYPE"),
                    foreign_currency=foreign,
                    amount_currency=amount_cur,
                    narration=clean_text(name, memo, sep="\n") or None,
                )
            )
        ledger = stmtrs.find("LEDGERBAL")
        if ledger is not None and _t(ledger, "BALAMT"):
            st.balance_end = to_decimal(_t(ledger, "BALAMT"))
            st.date = _date(_t(ledger, "DTASOF"))
        if tranlist is not None and st.date is None:
            st.date = _date(_t(tranlist, "DTEND"))
        st.name = f"OFX {st.date.isoformat()}" if st.date else "OFX"
        st.complete_balances()
        statements.append(st)
    if not statements:
        raise UserError(_("The OFX file contains no bank or credit-card statement"))
    return statements
