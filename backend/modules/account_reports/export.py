"""Report exports: CSV, XLSX (openpyxl) and PDF (the ``reports`` module's layouts and WeasyPrint).

Exports unfold every line; ledgers include up to ``EXPORT_LIMIT`` items per account / partner (a warning
row says when more exist)."""

from __future__ import annotations

import csv
import io
from typing import Any

from modules.account_reports.engine import run_report
from modules.account_reports.params import ReportParams
from tessera.exceptions import UserError
from tessera.i18n import _

EXPORT_LIMIT = 20_000
FORMATS = {
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "html": "text/html; charset=utf-8",
}

PDF_BODY = """
<h1>{{ report.report.name }}</h1>
<p>{{ report.companies | map(attribute='name') | join(', ') }} · {{ report.currency.name }}
{% for w in report.warnings %}<br/><em>{{ w }}</em>{% endfor %}</p>
<table class="data report">
<thead>
{% if groups %}<tr><th></th>{% for g in groups %}
<th colspan="{{ g.span }}" class="num">{{ g.label }}</th>{% endfor %}</tr>{% endif %}
<tr><th></th>{% for c in report.columns %}
<th class="{{ 'num' if c.type == 'monetary' else '' }}">{{ c.label }}</th>{% endfor %}</tr>
</thead>
<tbody>
{% for line in rows %}<tr class="{{ line.style }}">
<td style="padding-left: {{ 4 + 12 * line.level }}px">{{ line.name }}</td>
{% for v in line.cells %}<td class="{{ 'num' if v.num else '' }}">{{ v.text }}</td>{% endfor %}
</tr>{% endfor %}
</tbody></table>
"""
PDF_CSS = """
table.report td.num, table.report th.num { text-align: right; white-space: nowrap; }
table.report tr.total td, table.report tr.header td { font-weight: bold; }
table.report tr.total td { border-top: 1px solid #555; }
table.report tr.item td { font-size: 8pt; color: #444; }
"""


def _cell(value: Any) -> Any:
    """Text a spreadsheet would evaluate (``=``, ``+``, ``-``, ``@``, tab, CR first) is exported as text:
    partner names, labels and references come from users, imported statements and e-invoices (U-9 SEC-55)."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    return [line for line in data["lines"] if line.get("style") != "load_more"]


def _text(value: Any, column: dict[str, Any], places: int) -> str:
    if value is None:
        return ""
    if column.get("type") == "monetary":
        return f"{float(value):,.{places}f}"
    return str(value)


def export_report(env: Any, params: ReportParams, fmt: str) -> tuple[bytes, str, str]:
    """Render the report for download: (content, mimetype, filename)."""
    if fmt not in FORMATS:
        raise UserError(_("Unknown export format '{format}'", format=fmt))
    params = params.model_copy(update={"unfold_all": True, "limit": EXPORT_LIMIT, "expand": None, "offset": 0})
    data = run_report(env, params)
    if any(line.get("style") == "load_more" for line in data["lines"]):
        data["warnings"].append(str(_("Some ledgers were cut at {limit} items", limit=EXPORT_LIMIT)))
    stem = f"{data['report']['code']}_{data['periods'][0]['date_to']}"
    if fmt == "csv":
        return _csv(data), FORMATS[fmt], f"{stem}.csv"
    if fmt == "xlsx":
        return _xlsx(data), FORMATS[fmt], f"{stem}.xlsx"
    html_doc = _html(env, data)
    if fmt == "html":
        return html_doc.encode(), FORMATS[fmt], f"{stem}.html"
    from tessera.reports import render_pdf

    return render_pdf(html_doc), FORMATS[fmt], f"{stem}.pdf"


def _headers(data: dict[str, Any]) -> list[str]:
    return [
        " ".join(x for x in (c.get("group"), c["label"]) if x) if c.get("group") else c["label"]
        for c in data["columns"]
    ]


def _csv(data: dict[str, Any]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([_cell(data["report"]["name"]), *map(_cell, _headers(data))])
    for line in _rows(data):
        writer.writerow(
            [
                "  " * int(line["level"]) + str(_cell(str(line["name"]))),
                *("" if v is None else _cell(v) for v in line["values"]),
            ]
        )
    return ("﻿" + buffer.getvalue()).encode()


def _xlsx(data: dict[str, Any]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font

    places = int(data["currency"]["decimal_places"])
    number_format = "#,##0" + ("." + "0" * places if places else "")
    book = Workbook()
    sheet = book.active
    sheet.title = str(data["report"]["name"])[:31]
    sheet.append([_cell(data["report"]["name"])])
    sheet["A1"].font = Font(bold=True, size=14)
    sheet.append([_cell(", ".join(c["name"] for c in data["companies"])), data["currency"]["name"]])
    for warning in data["warnings"]:
        sheet.append([_cell(warning)])
    sheet.append([])
    sheet.append(["", *map(_cell, _headers(data))])
    for cell in sheet[sheet.max_row]:
        cell.font = Font(bold=True)
    columns = data["columns"]
    for line in _rows(data):
        sheet.append([_cell(str(line["name"])), *map(_cell, line["values"])])
        row = sheet[sheet.max_row]
        row[0].alignment = Alignment(indent=int(line["level"]))
        bold = line.get("style") in ("total", "header")
        for cell, column in zip(row[1:], columns, strict=False):
            if column.get("type") == "monetary":
                cell.number_format = number_format
            if bold:
                cell.font = Font(bold=True)
        if bold:
            row[0].font = Font(bold=True)
    sheet.column_dimensions["A"].width = 48
    for index in range(len(columns)):
        letter = sheet.cell(row=1, column=index + 2).column_letter
        sheet.column_dimensions[letter].width = 16
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _html(env: Any, data: dict[str, Any]) -> str:
    from tessera.reports import render_html
    from tessera.reports.engine import wrap

    places = int(data["currency"]["decimal_places"])
    columns = data["columns"]
    groups: list[dict[str, Any]] = []
    for column in columns:
        label = column.get("group") or ""
        if groups and groups[-1]["label"] == label:
            groups[-1]["span"] += 1
        else:
            groups.append({"label": label, "span": 1})
    rows = [
        {
            "name": line["name"],
            "level": line["level"],
            "style": line.get("style") or "normal",
            "cells": [
                {"text": _text(v, c, places), "num": c.get("type") == "monetary"}
                for v, c in zip(line["values"], columns, strict=False)
            ],
        }
        for line in _rows(data)
    ]
    company = env["core.company"].elevated().browse([data["companies"][0]["id"]])
    layouts = env["reports.layout"].elevated().search([("active", "=", True)])
    layout = next((lay for lay in layouts if lay.company_id and lay.company_id.ids == company.ids), None) or next(
        (lay for lay in layouts if not lay.company_id), None
    )
    layout_dict = dict(layout.as_dict()) if layout else {}
    if len(columns) > 5:
        layout_dict["orientation"] = "landscape"
    layout_dict["extra_css"] = (layout_dict.get("extra_css") or "") + PDF_CSS
    context = {
        "docs": None,
        "report": data,
        "rows": rows,
        "groups": groups if any(g["label"] for g in groups) else [],
        "company": wrap(company),
        "user": wrap(env.user.elevated()),
        "lang": env.lang,
    }
    return render_html(PDF_BODY, context, layout=layout_dict, title=str(data["report"]["name"]))
