"""Import / export (1.3-i, 1.3-j)."""

from __future__ import annotations

import csv
import io
import time
from typing import Any

import pytest

from tessera.config import get_settings
from tessera.dataio import export_rows, read_table, render_export, run_import, suggest_mapping
from tessera.jobs import run_now
from tessera.orm.environment import SYSTEM_UID, environment_for

pytestmark = pytest.mark.db


def _csv(rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode()


def test_read_table_csv_and_xlsx() -> None:
    headers, rows = read_table(b"name;color\nRed;1\nBlue;2\n\n", "t.csv")
    assert headers == ["name", "color"] and rows == [["Red", "1"], ["Blue", "2"]]
    content, _, _ = render_export(["name", "color"], [["Red", 1], ["Blue", 2]], "xlsx")
    headers, rows = read_table(content, "t.xlsx")
    assert headers == ["name", "color"] and rows == [["Red", "1"], ["Blue", "2"]]


def test_mapping_suggestions(env: Any) -> None:
    book = env["ktest.book"]
    assert suggest_mapping(env["ktest.author"], ["Author Name"]) == {"Author Name": "name"}  # model prefix stripped
    mapping = suggest_mapping(book, ["External ID", "Title", "Author", "Tags/id", "Page count", "Publish date", "zzz"])
    assert mapping == {
        "External ID": "id",
        "Title": "name",
        "Author": "author_id",
        "Tags/id": "tag_ids/id",
        "Page count": "page_count",
        "Publish date": "published",
        "zzz": None,
    }


def test_conversions_relations_and_external_ids(env: Any) -> None:
    author = env["ktest.author"].create({"name": "Ursula Import"})
    env["ktest.tag"].create({"name": "classic"})
    headers = ["id", "Title", "Author", "Tags", "Pages", "Status", "Published", "Price"]
    rows = [
        ["imp_book_1", "Earthsea", "Ursula Import", "classic", "250", "In review", "1968-11-01", "1,234.50"],
        ["imp_book_2", "Unknown author", "Nobody Here", "", "x", "nope", "not a date", "abc"],
    ]
    mapping = {
        "id": "id",
        "Title": "name",
        "Author": "author_id",
        "Tags": "tag_ids",
        "Pages": "page_count",
        "Status": "status",
        "Published": "published",
        "Price": "price",
    }
    result = run_import(env, "ktest.book", headers, rows, mapping)
    assert result.created == 1
    by_col = {e["column"]: e for e in result.errors}
    assert set(by_col) == {"Author", "Pages", "Status", "Published", "Price"} and all(
        e["row"] == 3 for e in result.errors
    )
    book = env.ref("__import__.imp_book_1")
    assert book.author_id == author and book.tag_ids.mapped("name") == ["classic"] and book.status == "review"
    assert book.price == 1234.5 and book.published.isoformat() == "1968-11-01"
    # same external id again → update, not duplicate
    again = run_import(env, "ktest.book", ["id", "Pages"], [["imp_book_1", "300"]], {"id": "id", "Pages": "page_count"})
    assert (again.created, again.updated) == (0, 1) and book.page_count == 300


def test_unique_constraint_error_names_the_column(env: Any) -> None:
    env["ktest.tag"].create({"name": "dup-tag"})
    result = run_import(env, "ktest.tag", ["Name"], [["dup-tag"]], {"Name": "name"})
    assert result.errors == [{"row": 2, "column": "Name", "field": "name", "message": "Tag names must be unique"}]


def test_required_field_must_be_mapped(env: Any) -> None:
    result = run_import(env, "ktest.tag", ["color"], [["1"]], {"color": "color"})
    assert result.created == 0 and "Required fields not mapped" in result.errors[0]["message"]


def _big_file(n: int = 10_000) -> tuple[list[list[Any]], set[tuple[int, str]]]:
    rows: list[list[Any]] = [["Name", "Color"]]
    expected: set[tuple[int, str]] = set()
    bad_color = {7 + 613 * k for k in range(15)}  # 15 values that are not numbers
    no_name = {300 + 911 * k for k in range(10)}  # 10 rows missing the required name
    for i in range(n):
        name, color = f"bulk-{i}", str(i % 12)
        if i in bad_color:
            color = f"red{i}"
            expected.add((i + 2, "Color"))
        elif i in no_name:
            name = ""
            expected.add((i + 2, "Name"))
        rows.append([name, color])
    assert len(expected) == 25
    return rows, expected


def test_dry_run_reports_exact_errors_and_writes_nothing(env: Any) -> None:
    """1.3-i (first half): 10,000 rows, 25 deliberate errors, nothing written."""
    data, expected = _big_file()
    before = env["ktest.tag"].search_count([])
    started = time.perf_counter()
    result = run_import(env, "ktest.tag", data[0], data[1:], {"Name": "name", "Color": "color"}, dry_run=True)
    elapsed = time.perf_counter() - started
    assert {(e["row"], e["column"]) for e in result.errors} == expected
    assert len(result.errors) == 25 and result.created == 9975
    assert env["ktest.tag"].search_count([]) == before  # dry run wrote nothing
    assert elapsed < 60


def test_real_import_runs_in_background_with_progress(registry: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """1.3-i (second half): the real run creates 9,975 records in a job and reports progress."""
    import tessera.realtime

    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        tessera.realtime,
        "publish",
        lambda db, channel, event, data=None: events.append({"channel": channel, "event": event, **(data or {})}),
    )
    data, _ = _big_file()
    url = get_settings().database_url
    with environment_for(url, 2) as env:  # admin
        imp = env["core.import_job"].create(
            {"model_name": "ktest.tag", "filename": "big.csv", "mapping": {"Name": "name", "Color": "color"}}
        )
        att = env["core.attachment"].create(
            {"name": "big.csv", "model": "core.import_job", "res_id": imp.ids[0], "raw": _csv(data)}
        )
        imp.write({"attachment_id": att.ids[0]})
        job_id = imp.action_start()["job_id"]
        imp_id = imp.ids[0]
    try:
        assert run_now(url, [job_id]) == {job_id: "done"}
        with environment_for(url, SYSTEM_UID, su=True) as env:
            imp = env["core.import_job"].browse([imp_id])
            assert (imp.state, imp.created_count, imp.error_count) == ("done", 9975, 25)
            assert env["ktest.tag"].search_count([("name", "like", "bulk-")]) == 9975
            ledger = env["core.job"].browse([job_id])
            assert ledger.progress == 100
        progress = [e for e in events if e["channel"] == f"job:{job_id}" and e["event"] == "progress"]
        assert len(progress) == 20 and progress[-1]["progress"] == 100
    finally:
        with environment_for(url, SYSTEM_UID, su=True) as env:
            env.cr.execute("DELETE FROM ktest_tag WHERE name LIKE 'bulk-%'")
            env.cr.execute("DELETE FROM core_import_job WHERE id = :id", {"id": imp_id})
            env.cr.execute("DELETE FROM core_job WHERE id = :id", {"id": job_id})


def test_export_drops_protected_fields(env: Any, env_for: Any) -> None:
    """1.3-j: secret_notes (group system) is absent for a reader even when selected."""
    env["ktest.author"].create({"name": "Secretive", "secret_notes": "classified"})
    reader = env_for("reader")
    authors = reader["ktest.author"].search([("name", "=", "Secretive")])
    kept, headers, rows = export_rows(authors, ["name", "secret_notes", "country_id/code", "book_ids/name"])
    assert kept == ["name", "country_id/code", "book_ids/name"] and rows[0][0] == "Secretive"
    assert headers == ["Name", "Country / ISO code", "Books / Title"]
    assert "classified" not in str(rows)
    admin_kept, _, admin_rows = export_rows(
        env["ktest.author"].search([("name", "=", "Secretive")]), ["name", "secret_notes"]
    )
    assert admin_kept == ["name", "secret_notes"] and admin_rows[0][1] == "classified"


def test_csv_export_neutralises_formulas() -> None:
    content, _, _ = render_export(["name"], [['=HYPERLINK("http://x")'], ["-1"], ["ok"]], "csv")
    text = content.decode("utf-8-sig")
    assert "'=HYPERLINK" in text and "'-1" in text


def test_http_upload_dry_run_and_export(librarian, reader) -> None:  # type: ignore[no-untyped-def]
    import uuid

    tag = f"http-imp-{uuid.uuid4().hex[:8]}"
    content = _csv([["Name", "Colour"], [tag, "3"], [f"{tag}-2", "oops"]])
    up = librarian.post(
        "/api/v1/import/ktest.tag", files={"file": ("tags.csv", content, "text/csv")}, data={"options": "{}"}
    )
    assert up.status_code == 201, up.text
    payload = up.json()
    assert payload["mapping"] == {"Name": "name", "Colour": "color"} and payload["row_count"] == 2
    jid = payload["id"]
    saved = librarian.patch(f"/api/v1/import/jobs/{jid}", json={"save_as": "Tag sheet"}).json()
    assert saved["mapping"]["Colour"] == "color"
    dry = librarian.post(f"/api/v1/import/jobs/{jid}/dry-run").json()
    assert dry["state"] == "tested" and dry["result"]["errors"] == [
        {"row": 3, "column": "Colour", "field": "color", "message": "'oops' is not a whole number"}
    ]
    assert reader.get(f"/api/v1/import/jobs/{jid}").status_code in (403, 404)  # someone else's import
    # a second upload with the same headers picks up the saved mapping
    again = librarian.post("/api/v1/import/ktest.tag", files={"file": ("tags2.csv", content, "text/csv")}).json()
    assert again["saved_mapping"] == "Tag sheet"
    started = librarian.post(f"/api/v1/import/jobs/{jid}/start")
    assert started.status_code == 202 and started.json()["state"] == "queued"
    exp = reader.post(
        "/api/v1/export", json={"model": "ktest.author", "fields": ["name", "secret_notes"], "format": "csv"}
    )
    assert exp.status_code == 200 and exp.headers["X-Tessera-Dropped-Fields"] == "secret_notes"
    assert exp.content.decode("utf-8-sig").splitlines()[0] == "Name"
    fields = reader.get("/api/v1/export/ktest.author/fields").json()
    assert "secret_notes" not in {f["path"] for f in fields} and "country_id" in {f["path"] for f in fields}
    template = librarian.get("/api/v1/import/ktest.tag/template")
    assert template.status_code == 200 and template.content.decode("utf-8-sig").startswith("Name")
