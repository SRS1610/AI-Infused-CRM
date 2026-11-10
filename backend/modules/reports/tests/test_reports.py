"""Reports (1.3-k): per-company layouts, QR codes that decode, versioning, sandboxing, access."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from tessera.exceptions import AccessError, UserError, ValidationError
from tessera.reports import blocks_to_template, render_html

pytestmark = pytest.mark.db

BODY = """
<h1>{{ doc.name }}</h1>
<p>Author: {{ doc.author_id.name if doc.author_id else '—' }}</p>
<p>Pages: {{ doc.page_count }} · Price: {{ doc.price | money(doc.currency_id) }}</p>
{{ qr('https://verify.example.test/book/' ~ doc.id) }}
"""


def _pdf_text(pdf: bytes) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "r.pdf"
        path.write_bytes(pdf)
        return subprocess.run(
            ["pdftotext", "-layout", str(path), "-"], capture_output=True, check=True, text=True
        ).stdout


def _decode_qr(pdf: bytes) -> list[str]:
    import zxingcpp
    from PIL import Image

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "r.pdf"
        path.write_bytes(pdf)
        subprocess.run(
            ["pdftoppm", "-r", "150", "-png", "-f", "1", "-l", "1", str(path), str(Path(tmp) / "page")], check=True
        )
        page = next(Path(tmp).glob("page*.png"))
        return [r.text for r in zxingcpp.read_barcodes(Image.open(page))]


def _template(env: Any, body: str = BODY) -> Any:
    tpl = env["reports.template"].create(
        {"name": "Book sheet", "model_name": "ktest.book", "filename": "Book {{ doc.name }}"}
    )
    tpl.publish_version(body=body, note="first")
    return tpl


needs_poppler = pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="poppler-utils not installed")


@needs_poppler
def test_pdf_with_company_layout_and_decodable_qr(env: Any) -> None:
    """1.3-k: per-company header/footer, QR decodes to the configured URL, render < 2 s."""
    eu = env.ref("core.demo_company_eu")
    env = env.with_context(allowed_company_ids=[env.ref("core.main_company").ids[0], eu.ids[0]])
    env["reports.layout"].elevated().create(
        {
            "name": "Europe",
            "company_id": eu.ids[0],
            "header_html": "<b>{{ company.name }}</b> — EU office",
            "footer_html": "Registered in France · {{ company.email }}",
            "paper": "A4",
        }
    )
    tpl = _template(env)
    main_book = env["ktest.book"].create({"name": "Main Book", "page_count": 120, "price": 19.9})
    eu_book = env["ktest.book"].with_company(eu).create({"name": "Livre EU", "page_count": 80, "price": 12.5})

    started = time.perf_counter()
    pdf, mimetype, filename = tpl.render(eu_book.ids)
    elapsed = time.perf_counter() - started
    assert mimetype == "application/pdf" and filename == "Book Livre EU.pdf" and pdf.startswith(b"%PDF")
    assert elapsed < 2.0, f"render took {elapsed:.2f}s"
    text = _pdf_text(pdf)
    assert "EU office" in text and "Registered in France" in text and "paris@example.com" in text
    assert "Livre EU" in text and "Page 1 / 1" in text
    assert _decode_qr(pdf) == [f"https://verify.example.test/book/{eu_book.ids[0]}"]

    main_text = _pdf_text(tpl.render(main_book.ids)[0])
    assert "EU office" not in main_text and "My Company" in main_text  # the standard layout


def test_versions_and_rollback(env: Any) -> None:
    tpl = _template(env, "<p>v1 {{ doc.name }}</p>")
    book = env["ktest.book"].create({"name": "Versioned"})
    assert tpl.publish_version(body="<p>v2 {{ doc.name }}</p>")["version"] == 2
    assert b"v2 Versioned" in tpl.render(book.ids, "html")[0]
    tpl.rollback_to(1)
    assert b"v1 Versioned" in tpl.render(book.ids, "html")[0]
    assert sorted(tpl.version_ids.mapped("number")) == [1, 2]
    with pytest.raises(ValidationError):
        tpl.publish_version(body="{% for x in %}")


@pytest.mark.parametrize(
    "body",
    [
        "{{ doc._rec }}",
        "{{ ''.__class__.__mro__ }}",
        "{{ doc.env }}",
        "{{ doc.unlink() }}",
        "{{ cycler.__init__.__globals__ }}",
        "{{ doc.no_such_field }}",
    ],
)
def test_sandbox_blocks_escapes(env: Any, body: str) -> None:
    tpl = _template(env, body)
    book = env["ktest.book"].create({"name": "Sandbox"})
    with pytest.raises(UserError, match="Template error"):
        tpl.render(book.ids, "html")
    assert book.exists()


def test_html_is_escaped(env: Any) -> None:
    tpl = _template(env, "<p>{{ doc.name }}</p>")
    book = env["ktest.book"].create({"name": "<script>alert(1)</script>"})
    html = tpl.render(book.ids, "html")[0].decode()
    assert "<script>alert" not in html and "&lt;script&gt;" in html


def test_template_reads_respect_field_access(env: Any, env_for: Any) -> None:
    tpl = env["reports.template"].create({"name": "Author", "model_name": "ktest.author"})
    tpl.publish_version(body="<p>{{ doc.name }} {{ doc.secret_notes }}</p>")
    author = env["ktest.author"].create({"name": "Hidden", "secret_notes": "classified"})
    assert b"classified" in tpl.render(author.ids, "html")[0]
    reader_tpl = tpl.with_env(env_for("reader"))
    with pytest.raises((AccessError, UserError)):
        reader_tpl.render(author.ids, "html")


def test_template_groups(env: Any, env_for: Any) -> None:
    tpl = _template(env)
    tpl.write({"group_ids": [("set", [env.ref("kernel_test_a.group_librarian").ids[0]])]})
    book = env["ktest.book"].create({"name": "Restricted", "status": "published"})
    with pytest.raises(AccessError):
        tpl.with_env(env_for("reader")).render(book.ids, "html")
    assert tpl.with_env(env_for("librarian")).render(book.ids, "html")[0]


def test_block_editor_compiles(env: Any) -> None:
    blocks = [
        {"type": "heading", "text": "Book <b>", "level": 1},
        {
            "type": "fields",
            "fields": [{"field": "name", "label": "Title"}, {"field": "author_id.name", "label": "Author"}],
        },
        {
            "type": "table",
            "source": "chapter_ids",
            "columns": [{"field": "name", "label": "Chapter"}, {"field": "words", "label": "Words"}],
        },
        {"type": "qr", "value": "https://example.test/x"},
        {"type": "spacer", "height": 5},
    ]
    body = blocks_to_template(blocks)
    assert "Book &lt;b&gt;" in body and "{% for line in doc.chapter_ids %}" in body
    author = env["ktest.author"].create({"name": "Block Author"})
    book = env["ktest.book"].create({"name": "Blocks", "author_id": author.ids[0]})
    env["ktest.chapter"].create({"book_id": book.ids[0], "name": "One", "words": 100})
    html = render_html(body, {"docs": [book]})
    assert "Block Author" in html and "<td>One</td><td>100</td>" in html and 'img class="qr"' in html
    with pytest.raises(UserError):
        blocks_to_template([{"type": "field", "field": "__class__"}])
    with pytest.raises(UserError):
        blocks_to_template([{"type": "shell"}])


def test_http_render_and_async(librarian, admin) -> None:  # type: ignore[no-untyped-def]
    from tessera.config import get_settings
    from tessera.jobs import run_now
    from tessera.orm.environment import SYSTEM_UID, environment_for

    url = get_settings().database_url
    with environment_for(url, SYSTEM_UID, su=True) as env:
        tpl = _template(env, "<h1>{{ doc.name }}</h1>")
        tpl_id = tpl.ids[0]
    try:
        book = librarian.post("/api/v1/models/ktest.book", json={"values": {"name": "HTTP Print"}}).json()
        listed = librarian.get("/api/v1/reports/for/ktest.book").json()
        assert any(t["id"] == tpl_id for t in listed)
        resp = librarian.get(f"/api/v1/reports/{tpl_id}/render", params={"ids": str(book["id"]), "format": "pdf"})
        assert resp.status_code == 200 and resp.content.startswith(b"%PDF")
        assert 'filename="Book HTTP Print.pdf"' in resp.headers["content-disposition"]
        assert librarian.get(f"/api/v1/reports/{tpl_id}/render", params={"ids": "x"}).status_code == 400
        job = librarian.post(f"/api/v1/reports/{tpl_id}/render-async", json={"ids": [book["id"]]}).json()
        assert run_now(url, [job["job_id"]]) == {job["job_id"]: "done"}
        with environment_for(url, SYSTEM_UID, su=True) as env:
            result = env["core.job"].browse([job["job_id"]]).result
            att = env["core.attachment"].browse([result["attachment_id"]])
            assert att.res_id == book["id"] and att.raw.startswith(b"%PDF")
        preview = admin.post(
            "/api/v1/reports/preview",
            json={"model": "ktest.book", "res_id": book["id"], "blocks": [{"type": "heading", "text": "Preview"}]},
        )
        assert preview.status_code == 200 and "<h2>Preview</h2>" in preview.text
        assert (
            librarian.post(
                "/api/v1/reports/preview", json={"model": "ktest.book", "res_id": book["id"], "body": "x"}
            ).status_code
            == 403
        )
    finally:
        with environment_for(url, SYSTEM_UID, su=True) as env:
            env["reports.template"].browse([tpl_id]).unlink()
