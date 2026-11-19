"""CSV bank exports with a configurable column mapping.

Options (all optional; the journal can remember them in ``bank_csv_options``):

* ``delimiter`` (sniffed), ``encoding`` (``utf-8-sig``, then ``cp1252``), ``skip_rows`` (lines before the header);
* ``columns``: ``{"date": "Booking date", "amount": "Amount", ...}`` mapping our fields to header names or
  0-based column numbers. Fields: ``date``, ``amount`` *or* ``debit`` + ``credit``, ``payment_ref``,
  ``partner_name``, ``account_number``, ``ref``, ``unique_import_id``, ``balance``, ``currency``,
  ``transaction_type``. Without a mapping the header row is detected and columns are recognised by name
  (English, French, German, Dutch, Spanish synonyms);
* ``date_format`` (``%d/%m/%Y`` …; detected from the values when absent), ``decimal_separator`` and
  ``thousands_separator`` (detected), ``debit_negative`` (debit column holds negative numbers);
* ``balance_start`` / ``balance_end`` (when the file has no running balance).
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import unicodedata
from decimal import Decimal
from typing import Any

from modules.account_bank.importers.base import (
    ParsedStatement,
    ParsedTransaction,
    clean_text,
    sanitize_account,
    to_decimal,
)
from tessera.exceptions import UserError
from tessera.i18n import _

SYNONYMS: dict[str, tuple[str, ...]] = {
    "date": ("date", "bookingdate", "transactiondate", "posted", "posteddate", "dateoperation", "dateop",
             "buchungstag", "buchungsdatum", "datum", "boekdatum", "fecha", "fechaoperacion", "valuedate",
             "datevaleur", "valutadatum", "valuta"),
    "amount": ("amount", "montant", "betrag", "bedrag", "importe", "transactionamount", "amounteur", "amountusd",
               "umsatz", "net"),
    "debit": ("debit", "debet", "withdrawal", "withdrawals", "moneyout", "paidout", "soll", "af", "cargo",
              "debitamount"),
    "credit": ("credit", "deposit", "deposits", "moneyin", "paidin", "haben", "bij", "abono", "creditamount"),
    "payment_ref": ("description", "label", "libelle", "memo", "details", "narrative", "communication",
                    "verwendungszweck", "omschrijving", "concepto", "reference text", "purpose", "text",
                    "transactiondetails", "remittanceinformation"),
    "partner_name": ("payee", "payer", "name", "counterparty", "counterpartyname", "beneficiary", "tiers",
                     "beguenstigterzahlungspflichtiger", "auftraggeber", "naam", "tegenpartij", "nombre",
                     "partner"),
    "account_number": ("iban", "counterpartyaccount", "account", "accountnumber", "kontonummer",
                       "tegenrekening", "cuenta", "counterpartyiban"),
    "ref": ("reference", "ref", "endtoendid", "endtoendreference", "referenz", "kenmerk", "referencia",
            "checknumber", "cheque"),
    "unique_import_id": ("id", "transactionid", "fitid", "uniqueid", "bankreference"),
    "balance": ("balance", "solde", "saldo", "runningbalance"),
    "currency": ("currency", "devise", "wahrung", "waehrung", "valuta currency", "moneda"),
    "transaction_type": ("type", "transactiontype", "buchungstext", "typeoperation", "code"),
}  # fmt: skip
DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d.%m.%Y", "%d-%m-%Y", "%Y/%m/%d", "%d/%m/%y", "%m/%d/%y",
                "%d.%m.%y", "%Y%m%d")  # fmt: skip


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _decode(content: bytes, encoding: str | None) -> str:
    for enc in [encoding] if encoding else ["utf-8-sig", "cp1252"]:
        try:
            return content.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return content.decode("latin-1")


def _guess_columns(header: list[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    normalised = [_norm(h) for h in header]
    for field, names in SYNONYMS.items():
        keys = [_norm(n) for n in names]
        for key in keys:  # synonyms are in order of preference
            if key in normalised and normalised.index(key) not in found.values():
                found[field] = normalised.index(key)
                break
    return found


def _resolve(columns: dict[str, Any], header: list[str] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    normalised = [_norm(h) for h in header] if header else []
    for field, spec in columns.items():
        if spec is None or spec == "":
            continue
        if isinstance(spec, int) or (isinstance(spec, str) and spec.isdigit()):
            out[field] = int(spec)
        elif _norm(str(spec)) in normalised:
            out[field] = normalised.index(_norm(str(spec)))
        else:
            raise UserError(_("Column '{column}' is not in the file", column=spec))
    return out


def _detect_header(rows: list[list[str]]) -> int:
    """Index of the header row: the first row in which a date and an amount column are recognised."""
    for i, row in enumerate(rows[:15]):
        cols = _guess_columns(row)
        if "date" in cols and ("amount" in cols or ("debit" in cols and "credit" in cols)):
            return i
    raise UserError(_("No header row with a date and an amount column was found; give the column mapping"))


def _date_format(values: list[str], forced: str | None) -> str:
    if forced:
        return forced
    for fmt in DATE_FORMATS:
        try:
            for v in values:
                dt.datetime.strptime(v.strip()[:10] if fmt != "%Y%m%d" else v.strip(), fmt)
        except ValueError:
            continue
        return fmt
    raise UserError(_("Unrecognised date format (e.g. {value}); set date_format", value=values[0] if values else ""))


def _number_format(values: list[str]) -> tuple[str, str | None]:
    """(decimal separator, thousands separator) guessed from the amounts."""
    votes = {",": 0, ".": 0}
    for v in values:
        s = re.sub(r"[^0-9.,]", "", v)
        idx = max(s.rfind(","), s.rfind("."))
        if idx < 0:
            continue
        sep = s[idx]
        other = "." if sep == "," else ","
        # the last separator is the decimal one unless it is followed by exactly three digits (thousands)
        if len(s) - idx - 1 != 3 or other in s[:idx]:
            votes[sep] += 1
    if votes[","] > votes["."]:
        return ",", "."
    return ".", ","


def parse(content: bytes, options: dict[str, Any] | None = None) -> list[ParsedStatement]:
    opts = dict(options or {})
    text = _decode(content, opts.get("encoding"))
    lines = text.splitlines()[int(opts.get("skip_rows") or 0) :]
    sample = "\n".join(lines[:20])
    delimiter = opts.get("delimiter")
    if not delimiter:
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ";" if sample.count(";") > sample.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO("\n".join(lines)), delimiter=delimiter) if any(c.strip() for c in r)]
    if not rows:
        raise UserError(_("The CSV file is empty"))
    if opts.get("columns"):
        has_header = opts.get("has_header", True)
        header = rows[0] if has_header else None
        cols = _resolve(opts["columns"], header)
        data = rows[1:] if has_header else rows
    else:
        start = _detect_header(rows)
        header = rows[start]
        cols = _guess_columns(header)
        data = rows[start + 1 :]
    if "date" not in cols or not ("amount" in cols or ("debit" in cols or "credit" in cols)):
        raise UserError(_("The column mapping needs a date and an amount (or debit and credit) column"))

    def cell(row: list[str], field: str) -> str | None:
        idx = cols.get(field)
        if idx is None or idx >= len(row):
            return None
        return row[idx].strip() or None

    data = [r for r in data if cell(r, "date")]
    fmt = _date_format([cell(r, "date") or "" for r in data], opts.get("date_format"))
    amount_values = [v for r in data for f in ("amount", "debit", "credit", "balance") if (v := cell(r, f))]
    dec, thousands = (
        (opts["decimal_separator"], opts.get("thousands_separator"))
        if opts.get("decimal_separator")
        else _number_format(amount_values)
    )

    def num(value: str | None) -> Decimal:
        return to_decimal(value, decimal_sep=dec, thousands_sep=thousands) if value else Decimal(0)

    st = ParsedStatement(currency=opts.get("currency"))
    balances: list[tuple[dt.date, Decimal, Decimal]] = []
    for row in data:
        raw_date = cell(row, "date") or ""
        day = dt.datetime.strptime(raw_date[:10] if fmt != "%Y%m%d" else raw_date, fmt).date()
        if "amount" in cols:
            amount = num(cell(row, "amount"))
        else:
            debit, credit = num(cell(row, "debit")), num(cell(row, "credit"))
            amount = abs(credit) - abs(debit) if not opts.get("debit_negative") else credit + debit
        if cell(row, "currency") and not st.currency:
            st.currency = cell(row, "currency")
        st.transactions.append(
            ParsedTransaction(
                date=day,
                amount=amount,
                payment_ref=clean_text(cell(row, "payment_ref")) or clean_text(cell(row, "partner_name")) or "",
                partner_name=cell(row, "partner_name"),
                account_number=sanitize_account(cell(row, "account_number")),
                ref=cell(row, "ref"),
                unique_import_id=cell(row, "unique_import_id"),
                transaction_type=cell(row, "transaction_type"),
            )
        )
        if cell(row, "balance"):
            balances.append((day, amount, num(cell(row, "balance"))))
    if balances:
        # running balance: works whether the export is in ascending or descending date order
        first_row, last_row = balances[0], balances[-1]
        ascending = first_row[0] <= last_row[0]
        oldest, newest = (first_row, last_row) if ascending else (last_row, first_row)
        st.balance_start = oldest[2] - oldest[1]
        st.balance_end = newest[2]
    if opts.get("balance_start") not in (None, ""):
        st.balance_start = Decimal(str(opts["balance_start"]))
    if opts.get("balance_end") not in (None, ""):
        st.balance_end = Decimal(str(opts["balance_end"]))
    if st.transactions:
        st.date = max(t.date for t in st.transactions)
        st.name = f"CSV {st.date.isoformat()}"
    st.complete_balances()
    return [st]
