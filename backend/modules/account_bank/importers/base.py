"""Common structures of the statement parsers.

A parser turns a file into :class:`ParsedStatement` objects; it never touches the database. Amounts are
signed (money in > 0) in the statement currency; every transaction carries a ``unique_import_id`` that is
stable for the same file so that importing it again adds nothing (``UNIQUE (journal_id, unique_import_id)``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from lxml import etree

from tessera.exceptions import UserError
from tessera.i18n import _


@dataclass
class ParsedTransaction:
    date: dt.date
    amount: Decimal
    payment_ref: str = ""
    partner_name: str | None = None
    account_number: str | None = None
    ref: str | None = None
    unique_import_id: str | None = None
    transaction_type: str | None = None
    foreign_currency: str | None = None
    amount_currency: Decimal | None = None
    narration: str | None = None
    value_date: dt.date | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "amount": str(self.amount),
            "payment_ref": self.payment_ref,
            "partner_name": self.partner_name,
            "account_number": self.account_number,
            "ref": self.ref,
            "unique_import_id": self.unique_import_id,
            "transaction_type": self.transaction_type,
            "foreign_currency": self.foreign_currency,
            "amount_currency": str(self.amount_currency) if self.amount_currency is not None else None,
        }


@dataclass
class ParsedStatement:
    name: str | None = None
    date: dt.date | None = None
    currency: str | None = None
    account_number: str | None = None
    balance_start: Decimal | None = None
    balance_end: Decimal | None = None
    transactions: list[ParsedTransaction] = field(default_factory=list)
    balances_in_file: bool = True

    @property
    def total(self) -> Decimal:
        return sum((t.amount for t in self.transactions), Decimal(0))

    def complete_balances(self) -> None:
        """Derive a missing opening or closing balance; refuse a file whose balances do not add up."""
        if self.balance_start is None and self.balance_end is not None:
            self.balance_start = self.balance_end - self.total
        elif self.balance_end is None and self.balance_start is not None:
            self.balance_end = self.balance_start + self.total
        elif self.balance_start is None and self.balance_end is None:
            self.balances_in_file = False  # the importer continues from the journal's last statement
            self.balance_start = Decimal(0)
            self.balance_end = self.total
        assert self.balance_start is not None and self.balance_end is not None
        if abs(self.balance_start + self.total - self.balance_end) > Decimal("0.005"):
            raise UserError(
                _(
                    "Statement {name}: the opening balance {start} plus the transactions ({total}) does not "
                    "equal the closing balance {end}",
                    name=self.name or "",
                    start=self.balance_start,
                    total=self.total,
                    end=self.balance_end,
                )
            )
        if self.date is None and self.transactions:
            self.date = max(t.date for t in self.transactions)

    def fill_unique_ids(self, prefix: str) -> None:
        """Give transactions without a bank id a deterministic one (same content → same id)."""
        seen: dict[str, int] = {}
        for t in self.transactions:
            if t.unique_import_id:
                t.unique_import_id = f"{prefix}-{t.unique_import_id}" if prefix else t.unique_import_id
                continue
            raw = "|".join(
                [t.date.isoformat(), str(t.amount), t.payment_ref or "", t.ref or "", t.account_number or ""]
            )
            digest = hashlib.sha1(raw.encode()).hexdigest()[:16]  # noqa: S324 - identifier, not security
            seen[digest] = seen.get(digest, 0) + 1
            t.unique_import_id = f"{prefix}-{digest}-{seen[digest]}" if prefix else f"{digest}-{seen[digest]}"


def to_decimal(text: str | None, *, decimal_sep: str = ".", thousands_sep: str | None = None) -> Decimal:
    if text is None:
        raise ValueError("empty amount")
    value = re.sub(r"\s", "", text.strip())  # spaces, including no-break spaces used as thousands separators
    negative = False
    if value.startswith("(") and value.endswith(")"):
        negative, value = True, value[1:-1]
    if value.endswith("-"):
        negative, value = True, value[:-1]
    if thousands_sep:
        value = value.replace(thousands_sep, "")
    if decimal_sep != ".":
        value = value.replace(decimal_sep, ".")
    value = re.sub(r"[^0-9.+-]", "", value)
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"not an amount: {text!r}") from exc
    return -number if negative else number


def sanitize_account(number: str | None) -> str | None:
    if not number:
        return None
    clean = "".join(ch for ch in number if ch.isalnum()).upper()
    return clean or None


def safe_xml(content: bytes) -> etree._Element:
    """Parse untrusted XML: no DTD loading, no entity expansion, no network access."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        return etree.fromstring(content, parser=parser)
    except etree.XMLSyntaxError as exc:
        raise UserError(_("The file is not valid XML: {error}", error=str(exc))) from exc


def clean_text(*parts: str | None, sep: str = " ") -> str:
    return sep.join(" ".join(p.split()) for p in parts if p and p.strip())
