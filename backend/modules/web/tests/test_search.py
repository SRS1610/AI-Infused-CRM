"""Global search: index maintenance, ranking, and access filtering before results are returned."""

from __future__ import annotations

from typing import Any

import pytest

from modules.web.search import rebuild, search

pytestmark = pytest.mark.db


def _hits(env: Any, q: str) -> list[tuple[str, str]]:
    return [(h["model"], h["title"]) for h in search(env, q)]


def test_index_follows_create_write_delete_restore(env: Any) -> None:
    book = env["ktest.book"].create({"name": "Zephyrine Quasar Atlas"})
    assert ("ktest.book", "Zephyrine Quasar Atlas") in _hits(env, "zephyrine")
    book.write({"name": "Obsidian Quasar Atlas"})
    assert not _hits(env, "zephyrine") and _hits(env, "obsidian quasar")
    book.unlink()  # soft delete
    assert not _hits(env, "obsidian quasar")
    book.with_context(include_deleted=True).restore()
    assert _hits(env, "obsidian quasar")


def test_typos_and_ranking(env: Any) -> None:
    env["ktest.book"].create({"name": "Mandelbrot Sets Explained"})
    env["ktest.book"].create({"name": "A Short Note on Mandelbrot"})
    hits = _hits(env, "mandelbrot")
    assert [t for _, t in hits[:2]] == ["Mandelbrot Sets Explained", "A Short Note on Mandelbrot"]  # prefix first
    assert _hits(env, "mandlebrot sets")  # transposed letters still match (trigrams)


def test_results_respect_record_rules(env: Any, env_for: Any) -> None:
    env["ktest.book"].create({"name": "Quokka Draft Secrets", "status": "draft"})
    env["ktest.book"].create({"name": "Quokka Published Story", "status": "published"})
    reader = env_for("reader")  # readers see published books only
    assert [t for _, t in _hits(reader, "quokka")] == ["Quokka Published Story"]
    assert {t for _, t in _hits(env, "quokka")} == {"Quokka Draft Secrets", "Quokka Published Story"}
    demo = env_for("demo")  # no access to books at all
    assert _hits(demo, "quokka") == []


def test_rebuild(env: Any) -> None:
    book = env["ktest.book"].create({"name": "Rebuild Pangolin"})
    env.cr.execute("DELETE FROM web_search_index WHERE model = 'ktest.book'")
    assert not _hits(env, "pangolin")
    counts = rebuild(env.elevated(), ["ktest.book"])
    assert counts["ktest.book"] >= 1 and _hits(env, "pangolin")
    assert book.ids


def test_search_endpoint_returns_records_and_menus(librarian) -> None:  # type: ignore[no-untyped-def]
    librarian.post("/api/v1/models/ktest.book", json={"values": {"name": "Endpoint Okapi Book", "status": "published"}})
    out = librarian.get("/api/v1/web/search", params={"q": "okapi"}).json()
    assert out["records"][0]["title"] == "Endpoint Okapi Book" and out["records"][0]["model_label"] == "Book"
    menus = librarian.get("/api/v1/web/search", params={"q": "my acc"}).json()["menus"]
    assert menus and all(m["path"].startswith("My account / ") and m["action"] for m in menus)
    assert librarian.get("/api/v1/web/search", params={"q": "o"}).json() == {"records": [], "menus": []}
