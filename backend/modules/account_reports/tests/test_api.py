"""HTTP API and exports (CSV, XLSX, HTML, PDF)."""

from __future__ import annotations

import io

import pytest

from modules.account_reports.export import export_report
from modules.account_reports.params import ReportParams
from modules.account_reports.tests.common import D, entry
from tessera.exceptions import UserError

pytestmark = pytest.mark.db


def test_list_and_run(admin):
    listing = admin.get("/api/v1/account/reports")
    assert listing.status_code == 200, listing.text
    assert "balance_sheet" in {r["code"] for r in listing.json()}
    resp = admin.get(
        "/api/v1/account/reports/balance_sheet",
        params={"date_to": "2031-03-31", "comparison": "previous_year", "unfold": "line:1,line:2"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [c["date_to"] for c in data["columns"]] == ["2031-03-31", "2030-03-31"]
    assert data["params"]["unfold"] == ["line:1", "line:2"]
    run = admin.post("/api/v1/account/reports/run", json={"report": "profit_loss", "date_to": "2031-03-31"})
    assert run.status_code == 200, run.text
    assert run.json()["report"]["code"] == "profit_loss"
    schema = admin.get("/api/v1/account/reports/schema").json()
    assert "comparison" in schema["properties"]


def test_bad_parameters(admin):
    assert admin.get("/api/v1/account/reports/profit_loss", params={"date_to": "31/03/2031"}).status_code == 400
    assert admin.get("/api/v1/account/reports/profit_loss", params={"company_ids": "a,b"}).status_code == 400
    assert admin.get("/api/v1/account/reports/profit_loss", params={"comparison": "weekly"}).status_code == 400
    assert admin.get("/api/v1/account/reports/nothing").status_code == 404
    bad = admin.post("/api/v1/account/reports/run", json={"report": "profit_loss", "colour": "red"})
    assert bad.status_code == 422


def test_exports_over_http(admin):
    for fmt, mimetype in (
        ("csv", "text/csv"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("html", "text/html"),
    ):
        resp = admin.get(
            "/api/v1/account/reports/trial_balance/export", params={"format": fmt, "date_to": "2031-03-31"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith(mimetype)
        assert "trial_balance_2031-03-31" in resp.headers["content-disposition"]
    assert admin.get("/api/v1/account/reports/trial_balance/export", params={"format": "doc"}).status_code == 422


def test_reports_need_accounting_rights(reader):
    assert reader.get("/api/v1/account/reports/balance_sheet").status_code == 403
    assert reader.get("/api/v1/account/reports").status_code == 403


def test_export_contents(env):
    entry(env, D(2031, 1, 5), [("1000", 1234.5), ("3000", -1234.5)], ref="export")
    params = ReportParams(report="general_ledger", date_from=D(2031, 1, 1), date_to=D(2031, 1, 31))
    content, mimetype, filename = export_report(env, params, "csv")
    text = content.decode("utf-8-sig")
    assert filename == "general_ledger_2031-01-31.csv" and mimetype.startswith("text/csv")
    assert "1000 Bank" in text and "1234.5" in text and "Initial balance" in text
    content, _m, _f = export_report(env, params.model_copy(update={"report": "trial_balance"}), "xlsx")
    from openpyxl import load_workbook

    sheet = load_workbook(io.BytesIO(content)).active
    values = [cell.value for row in sheet.iter_rows() for cell in row]
    assert "1000 Bank" in values and 1234.5 in values
    html, _m, _f = export_report(env, params.model_copy(update={"report": "balance_sheet"}), "html")
    assert b"Balance sheet" in html and b"1,234.50" in html
    pdf, mimetype, _f = export_report(env, params.model_copy(update={"report": "profit_loss"}), "pdf")
    assert pdf.startswith(b"%PDF") and mimetype == "application/pdf"
    with pytest.raises(UserError):
        export_report(env, params, "doc")


def test_export_marks_cut_ledgers(env, monkeypatch):
    from modules.account_reports import export

    for day in (5, 6, 7):
        entry(env, D(2031, 1, day), [("1000", 1.0), ("3000", -1.0)], ref="cut")
    monkeypatch.setattr(export, "EXPORT_LIMIT", 2)
    params = ReportParams(report="general_ledger", date_from=D(2031, 1, 1), date_to=D(2031, 1, 31))
    content, _m, _f = export.export_report(env, params, "csv")
    assert b"Load more" not in content
    html, _m, _f = export.export_report(env, params, "html")
    assert b"cut at 2 items" in html
