"""Reading CSV and XLSX files into (headers, rows)."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _

MAX_ROWS = 200_000


def _decode(data: bytes, encoding: str | None) -> str:
    for enc in [encoding] if encoding else ["utf-8-sig", "utf-8", "cp1252", "latin-1"]:
        try:
            return data.decode(enc or "utf-8")
        except (UnicodeDecodeError, LookupError):
            continue
    raise UserError(_("Cannot decode the file: pass its encoding explicitly"))


def read_table(
    data: bytes,
    filename: str = "",
    *,
    encoding: str | None = None,
    delimiter: str | None = None,
    sheet: str | None = None,
) -> tuple[list[str], list[list[Any]]]:
    """Return the header row and the data rows. Empty trailing rows are dropped."""
    name = filename.lower()
    if name.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        return _read_xlsx(data, sheet)
    text = _decode(data, encoding)
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows = list(reader)
    return _split(rows)


def _read_xlsx(data: bytes, sheet: str | None) -> tuple[list[str], list[list[Any]]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise UserError(_("Not a valid XLSX file: {exc}", exc=exc)) from exc
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows: list[list[Any]] = []
    for row in ws.iter_rows(values_only=True):
        rows.append([_cell(v) for v in row])
        if len(rows) > MAX_ROWS + 1:
            break
    wb.close()
    return _split(rows)


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, dt.datetime) and value.time() == dt.time(0):
        return value.date().isoformat()
    if isinstance(value, dt.date | dt.datetime):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _split(rows: list[list[Any]]) -> tuple[list[str], list[list[Any]]]:
    while rows and not any(str(c).strip() for c in rows[-1]):
        rows.pop()
    if not rows:
        raise UserError(_("The file is empty"))
    headers = [str(h).strip() for h in rows[0]]
    data = rows[1:]
    if len(data) > MAX_ROWS:
        raise UserError(_("Files are limited to {limit} rows", limit=MAX_ROWS))
    width = len(headers)
    return headers, [list(r) + [""] * (width - len(r)) if len(r) < width else list(r[:width]) for r in data]
