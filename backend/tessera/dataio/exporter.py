"""Export records to rows (field paths through relations) and render CSV / XLSX. Field-level access is
enforced: a path through a field the user may not read is dropped, even when it was requested."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import TYPE_CHECKING, Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import fields as F

if TYPE_CHECKING:
    from tessera.orm.model import Model

MAX_EXPORT_ROWS = 100_000


def _allowed_path(model: Model, path: str) -> bool:
    current = model
    parts = path.split("/")
    for i, part in enumerate(parts):
        if part in ("id", ".id") and i == len(parts) - 1:
            return True
        f = current._fields.get(part)
        if f is None or not current._check_field_access(f, "read"):
            return False
        if i < len(parts) - 1:
            if not isinstance(f, F.Many2one | F.Many2many | F.One2many):
                return False
            current = current.env[str(f.comodel_name)]
            if not current.check_access_rights("read", raise_exception=False):
                return False
    return True


def _label(model: Model, path: str) -> str:
    labels = []
    current = model
    for part in path.split("/"):
        if part in ("id", ".id"):
            labels.append("External ID" if part == "id" else "ID")
            break
        f = current._fields[part]
        labels.append(f.label)
        if isinstance(f, F.Many2one | F.Many2many | F.One2many):
            current = current.env[str(f.comodel_name)]
    return " / ".join(labels)


def _value(record: Model, path: str) -> Any:
    head, _, rest = path.partition("/")
    if head == ".id":
        return record.ids[0] if record.ids else None
    if head == "id":
        return record.get_external_id().get(record.ids[0]) if record.ids else None
    value = record[head]
    f = record._fields[head]
    if isinstance(f, F.Many2one | F.Many2many | F.One2many):
        if rest:
            values = [_value(r, rest) for r in value]
            values = [v for v in values if v not in (None, "")]
            return ", ".join(str(v) for v in values) if len(values) != 1 else values[0]
        return ", ".join(value.mapped("display_name"))
    if isinstance(f, F.Selection) and isinstance(f.selection, list | tuple):
        return dict(f.selection).get(value, value) if value else ""
    return value


def export_rows(records: Model, paths: list[str]) -> tuple[list[str], list[str], list[list[Any]]]:
    """(kept paths, header labels, rows). Paths the user may not read are silently removed."""
    if len(records) > MAX_EXPORT_ROWS:
        raise UserError(_("Exports are limited to {limit} rows; narrow the selection", limit=MAX_EXPORT_ROWS))
    records.check_access("read")
    kept = [p for p in paths if _allowed_path(records, p)]
    headers = [_label(records, p) for p in kept]
    rows = [[_value(rec, p) for p in kept] for rec in records]
    return kept, headers, rows


def _scalar(value: Any) -> Any:
    if value is None or value is False:
        return ""
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None).isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value  # never let a spreadsheet evaluate exported text as a formula
    return value


def render_export(headers: list[str], rows: list[list[Any]], fmt: str = "csv") -> tuple[bytes, str, str]:
    """(content, mimetype, extension)."""
    if fmt == "xlsx":
        from openpyxl import Workbook

        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Export")
        ws.append(headers)
        for row in rows:
            ws.append([_scalar(v) for v in row])
        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_scalar(v) for v in row])
    return out.getvalue().encode("utf-8-sig"), "text/csv", "csv"
