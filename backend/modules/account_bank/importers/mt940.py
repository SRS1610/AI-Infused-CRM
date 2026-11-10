"""SWIFT MT940 customer statements (``:20: :25: :28C: :60F/M: :61: :86: :62F/M:``).

A file may hold several statements (one per ``:20:`` block, optionally wrapped in SWIFT ``{1:}{2:}{4:}``
blocks). ``:86:`` information may span several lines and is read in the three common layouts: German
``?NN`` sub-fields, SWIFT/SEPA ``/KEY/value`` codes, or free text.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal

from modules.account_bank.importers.base import (
    ParsedStatement,
    ParsedTransaction,
    clean_text,
    sanitize_account,
    to_decimal,
)
from tessera.exceptions import UserError
from tessera.i18n import _

TAG_RE = re.compile(r"^:(\d{2}[A-Z]?):(.*)$")
BAL_RE = re.compile(r"^(?P<sign>[CD])(?P<date>\d{6})(?P<cur>[A-Z]{3})(?P<amount>[\d,.]+)$")
LINE_RE = re.compile(
    r"^(?P<date>\d{6})(?P<entry>\d{4})?(?P<sign>R?[CD])(?P<funds>[A-Z])?(?P<amount>\d[\d,]*)"
    r"(?P<type>[NFS][A-Z0-9]{3})(?P<custref>[^/]*?)(?://(?P<bankref>.*))?$"
)
SLASH_KEYS = (
    "EREF", "NAME", "REMI", "IBAN", "BIC", "ORDP", "BENM", "CNTP", "MARF", "CSID", "TRTP", "PURP", "ADDR",
    "ULTC", "ULTD", "SVWZ", "KREF", "RTRN",
)  # fmt: skip
SLASH_RE = re.compile(r"/(" + "|".join(SLASH_KEYS) + r")/")


def is_mt940(content: bytes) -> bool:
    head = content[:4000]
    return b":20:" in head and (b":60F:" in head or b":60M:" in head or b":61:" in head)


def _yymmdd(text: str) -> dt.date:
    return dt.datetime.strptime(text, "%y%m%d").date()


def _amount(text: str) -> Decimal:
    return to_decimal(text, decimal_sep=",") if "," in text else to_decimal(text)


def _balance(value: str) -> tuple[Decimal, dt.date, str]:
    match = BAL_RE.match(value.strip())
    if not match:
        raise UserError(_("Unreadable MT940 balance: {value}", value=value))
    amount = _amount(match.group("amount"))
    return (-amount if match.group("sign") == "D" else amount), _yymmdd(match.group("date")), match.group("cur")


def _records(text: str) -> list[list[tuple[str, str]]]:
    """Split the file in statements, each a list of (tag, value) with continuation lines joined by newlines."""
    text = re.sub(r"\{[1-3]:[^}]*\}", "", text)
    text = text.replace("{4:", "\n").replace("-}", "\n-")
    statements: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r")
        if not line.strip():
            continue
        if line.strip() == "-":
            if current:
                statements.append(current)
            current = []
            continue
        match = TAG_RE.match(line)
        if match:
            if match.group(1) == "20" and any(tag == "20" for tag, _v in current):
                statements.append(current)
                current = []
            current.append((match.group(1), match.group(2)))
        elif current:
            tag, value = current[-1]
            current[-1] = (tag, f"{value}\n{line}")
    if current:
        statements.append(current)
    return [s for s in statements if any(tag == "61" or tag.startswith("60") for tag, _v in s)]


def parse_86(info: str) -> dict[str, str | None]:
    """Counterparty name, account, remittance text and end-to-end reference of a ``:86:`` field."""
    flat = info.replace("\n", "")
    out: dict[str, str | None] = {"name": None, "account": None, "label": None, "ref": None}
    if re.search(r"\?\d{2}", flat):
        fields: dict[str, list[str]] = {}
        for code, value in re.findall(r"\?(\d{2})([^?]*)", flat):
            fields.setdefault(code, []).append(value)
        purpose = "".join(v for c in [*map(str, range(20, 30)), *map(str, range(60, 64))] for v in fields.get(c, []))
        out["name"] = clean_text("".join(fields.get("32", [])) + "".join(fields.get("33", []))) or None
        out["account"] = sanitize_account("".join(fields.get("31", [])))
        eref = re.search(r"EREF\+(\S+?)(?=(SVWZ|KREF|MREF|CRED|ABWA|ABWE)\+|$)", purpose)
        svwz = re.search(r"SVWZ\+(.*?)(?=(EREF|KREF|MREF|CRED|ABWA|ABWE)\+|$)", purpose)
        out["ref"] = eref.group(1).strip() if eref else None
        out["label"] = clean_text(svwz.group(1) if svwz else purpose) or clean_text("".join(fields.get("00", [])))
        return out
    if SLASH_RE.search(flat):
        parts = SLASH_RE.split(flat)
        values: dict[str, str] = {}
        for key, value in zip(parts[1::2], parts[2::2], strict=False):
            values.setdefault(key, value.strip().strip("/"))
        if "CNTP" in values:  # /CNTP/account/bic/name/city/
            pieces = values["CNTP"].split("/")
            values.setdefault("IBAN", pieces[0] if pieces else "")
            values.setdefault("NAME", pieces[2] if len(pieces) > 2 else "")
        for side in ("BENM", "ORDP"):
            if side in values and "NAME" not in values:
                values["NAME"] = values[side].split("/")[-1]
        remi = values.get("REMI", "")
        if remi.startswith("USTD//") or remi.startswith("STRD/"):
            remi = remi.split("/", 2)[-1].lstrip("/")
        out["name"] = clean_text(values.get("NAME")) or None
        out["account"] = sanitize_account(values.get("IBAN"))
        out["label"] = clean_text(remi) or None
        out["ref"] = values.get("EREF") or None
        return out
    out["label"] = clean_text(info)
    return out


def parse(content: bytes) -> list[ParsedStatement]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    records = _records(text)
    if not records:
        raise UserError(_("The file is not an MT940 statement"))
    statements: list[ParsedStatement] = []
    for record in records:
        st = ParsedStatement()
        reference = number = None
        pending: ParsedTransaction | None = None
        counter = 0
        for tag, value in record:
            if tag == "20":
                reference = value.strip()
            elif tag == "25":
                st.account_number = sanitize_account(value.split("/")[-1] if "/" in value else value)
            elif tag == "28C":
                number = value.strip()
            elif tag in ("60F", "60M"):
                st.balance_start, _day, st.currency = _balance(value)
            elif tag in ("62F", "62M"):
                st.balance_end, st.date, cur = _balance(value)
                st.currency = st.currency or cur
            elif tag == "61":
                first, _sep, supplementary = value.partition("\n")
                match = LINE_RE.match(first.strip())
                if not match:
                    raise UserError(_("Unreadable MT940 transaction line: {value}", value=first))
                counter += 1
                sign = match.group("sign")
                amount = _amount(match.group("amount"))
                if sign in ("D", "RC"):
                    amount = -amount
                customer_ref = (match.group("custref") or "").strip()
                bank_ref = (match.group("bankref") or "").strip()
                unique = "-".join([reference or "", number or "", match.group("date"), bank_ref, str(counter)])
                pending = ParsedTransaction(
                    date=_yymmdd(match.group("date")),
                    amount=amount,
                    ref=None if customer_ref in ("", "NONREF") else customer_ref,
                    transaction_type=match.group("type"),
                    narration=supplementary.strip() or None,
                    unique_import_id=unique,
                )
                st.transactions.append(pending)
            elif tag == "86" and pending is not None:
                details = parse_86(value)
                pending.partner_name = details["name"]
                pending.account_number = details["account"]
                pending.payment_ref = details["label"] or pending.ref or ""
                if details["ref"]:
                    pending.ref = details["ref"]
                pending.narration = clean_text(pending.narration, value.replace("\n", ""), sep="\n") or None
        for t in st.transactions:
            if not t.payment_ref:
                t.payment_ref = t.ref or t.narration or ""
        st.name = f"{reference or 'MT940'} {number or ''}".strip()
        st.complete_balances()
        statements.append(st)
    return statements
