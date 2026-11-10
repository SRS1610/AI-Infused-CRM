"""ORM behaviour against a real PostgreSQL database."""

from __future__ import annotations

import datetime as dt

import pytest

from tessera.exceptions import MissingError, UserError, ValidationError

pytestmark = pytest.mark.db


# -- CRUD --------------------------------------------------------------------------------------


def test_create_read_write_unlink(env):  # type: ignore[no-untyped-def]
    author = env["ktest.author"].create({"name": "Ann Author"})
    book = env["ktest.book"].create(
        {"name": "Kernel Tales", "author_id": author.ids[0], "page_count": 100, "price": 9.99}
    )
    assert book.name == "Kernel Tales"
    assert book.author_id == author
    assert book.author_name == "Ann Author"
    assert book.price == 9.99
    assert book.currency_id.name == "USD"
    assert book.reference.startswith("BK/")
    assert book.create_uid.login == "admin"
    assert book.status == "draft"
    book.write({"name": "Kernel Tales II", "page_count": 120})
    assert book.name == "Kernel Tales II"
    read = book.read(["name", "author_id", "page_count", "tag_ids", "published"])
    assert read == [
        {
            "id": book.ids[0],
            "name": "Kernel Tales II",
            "author_id": {"id": author.ids[0], "display_name": "Ann Author"},
            "page_count": 120,
            "tag_ids": [],
            "published": None,
        }
    ]
    book.with_context(force_delete=True).unlink()
    assert not book.exists()
    with pytest.raises(MissingError):
        _ = env["ktest.book"].browse(book.ids).name


def test_batch_create_and_recordset_operations(env):  # type: ignore[no-untyped-def]
    books = env["ktest.book"].create([{"name": f"B{i}", "page_count": i * 100} for i in range(1, 4)])
    assert len(books) == 3
    assert books.mapped("name") == ["B1", "B2", "B3"]
    long_books = books.filtered(lambda b: b.page_count >= 200)
    assert long_books.mapped("name") == ["B2", "B3"]
    assert (books - long_books).mapped("name") == ["B1"]
    assert (books & long_books) == long_books
    assert books.sorted("page_count", reverse=True).mapped("name") == ["B3", "B2", "B1"]
    assert books[0].name == "B1"
    assert books[1:].mapped("name") == ["B2", "B3"]
    assert "name" in books
    assert books[0] in books


def test_required_and_selection_validation(env):  # type: ignore[no-untyped-def]
    with pytest.raises(ValidationError):
        env["ktest.book"].create({"page_count": 3})
    with pytest.raises(ValidationError):
        env["ktest.book"].create({"name": "x", "status": "nope"})
    with pytest.raises(ValidationError):
        env["ktest.book"].create({"name": "x", "page_count": -1})
    with pytest.raises(ValidationError):
        env["ktest.book"].create({"name": "x", "unknown_field": 1})


def test_defaults_and_context_defaults(env):  # type: ignore[no-untyped-def]
    book = env["ktest.book"].with_context(default_page_count=77).create({"name": "Ctx"})
    assert book.page_count == 77
    assert book.active is True
    assert book.company_id == env.company


# -- relations ----------------------------------------------------------------------------------


def test_many2many_commands(env):  # type: ignore[no-untyped-def]
    t1, t2, t3 = (env["ktest.tag"].create({"name": f"t{i}-{env.cr.dbname}"}) for i in range(3))
    book = env["ktest.book"].create({"name": "Tagged", "tag_ids": [t1.ids[0], t2.ids[0]]})
    assert set(book.tag_ids.ids) == {t1.ids[0], t2.ids[0]}
    assert book in t1.book_ids
    book.write({"tag_ids": [("add", t3.ids[0]), ("remove", t1.ids[0])]})
    assert set(book.tag_ids.ids) == {t2.ids[0], t3.ids[0]}
    assert book not in t1.book_ids
    book.write({"tag_ids": [("create", {"name": f"created-{env.cr.dbname}"})]})
    assert len(book.tag_ids) == 3
    book.write({"tag_ids": [("clear",)]})
    assert not book.tag_ids
    found = env["ktest.book"].search([("tag_ids", "in", [t2.ids[0]])])
    assert book not in found


def test_one2many_commands_and_stored_compute(env):  # type: ignore[no-untyped-def]
    book = env["ktest.book"].create(
        {
            "name": "Chapters",
            "chapter_ids": [
                ("create", {"name": "One", "words": 100}),
                ("create", {"name": "Two", "words": 250}),
            ],
        }
    )
    assert len(book.chapter_ids) == 2
    assert book.total_words == 350
    assert book.chapter_ids[0].book_title == "Chapters"
    first = book.chapter_ids[0]
    first.write({"words": 150})
    assert book.total_words == 400
    book.write({"chapter_ids": [("delete", first.ids[0])]})
    assert book.total_words == 250
    assert not first.exists()
    orphan = env["ktest.chapter"].create({"name": "Loose", "words": 10, "book_id": book.ids[0]})
    book.write({"chapter_ids": [("set", [])]})
    assert not orphan.exists()  # required inverse → child deleted on detach
    assert book.total_words == 0


def test_many2one_restrict_and_cascade(env):  # type: ignore[no-untyped-def]
    author = env["ktest.author"].create({"name": "Restricted"})
    book = env["ktest.book"].create(
        {"name": "Keep", "author_id": author.ids[0], "chapter_ids": [("create", {"name": "c", "words": 1})]}
    )
    with pytest.raises(UserError):
        author.unlink()
    chapter_ids = book.chapter_ids.ids
    book.with_context(force_delete=True).unlink()
    assert not env["ktest.chapter"].browse(chapter_ids).exists()
    author.unlink()
    assert not author.exists()


def test_related_fields_and_inverse_write(env):  # type: ignore[no-untyped-def]
    be = env.ref("core.country_be")
    author = env["ktest.author"].create({"name": "Related", "country_id": be.ids[0]})
    book = env["ktest.book"].create({"name": "Rel", "author_id": author.ids[0]})
    assert book.author_country_code == "BE"
    assert book.author_name == "Related"
    author.write({"name": "Renamed"})
    assert book.author_name == "Renamed"
    assert env["ktest.book"].search([("author_name", "=", "Renamed")]) == book
    assert env["ktest.book"].search([("author_id.country_id.code", "=", "BE")]) == book
    assert env.ref("kernel_test_a.book_musketeers") in env["ktest.book"].search(
        [("author_id.country_id.code", "=", "FR")]
    )


def test_stored_compute_dependency_through_many2one(env):  # type: ignore[no-untyped-def]
    author = env["ktest.author"].create({"name": "Counter"})
    assert author.book_count == 0
    b1 = env["ktest.book"].create({"name": "one", "author_id": author.ids[0]})
    assert author.book_count == 1
    b2 = env["ktest.book"].create({"name": "two", "author_id": author.ids[0]})
    assert author.book_count == 2
    other = env["ktest.author"].create({"name": "Other"})
    b2.write({"author_id": other.ids[0]})
    assert author.book_count == 1
    assert other.book_count == 1
    b1.with_context(force_delete=True).unlink()
    assert author.book_count == 0


def test_non_stored_compute_and_extension_override(env):  # type: ignore[no-untyped-def]
    book = env["ktest.book"].create({"name": "lower", "isbn": "978-3-16-148410-0"})
    assert book.title_upper == "LOWER [978-3-16-148410-0]"
    book.write({"name": "changed"})
    assert book.title_upper == "CHANGED [978-3-16-148410-0]"
    assert book.is_long is False
    book.write({"page_count": 600})
    assert book.is_long is True
    assert env["ktest.book"]._fields["name"].help == "Overridden help from module B"
    assert env["ktest.book"]._fields["name"].required is True  # attribute merge kept module A's required
    with pytest.raises(ValidationError):
        env["ktest.book"].create({"name": "bad", "isbn": "123"})
    with pytest.raises(ValidationError):
        book.write({"title_upper": "x"})  # computed without inverse


def test_method_override_chain(env):  # type: ignore[no-untyped-def]
    book = env["ktest.book"].create({"name": "Pub", "page_count": 0})
    book.action_publish()
    assert book.status == "published"
    assert book.page_count == 1  # module B's override ran after module A's
    assert "action_publish" in type(book)._exposed_methods


# -- translations & company dependent ---------------------------------------------------------


def test_translatable_fields(env):  # type: ignore[no-untyped-def]
    author = env["ktest.author"].create({"name": "Trans", "bio": "English bio"})
    author.with_context(lang="fr_FR").write({"bio": "Bio française"})
    assert author.bio == "English bio"
    assert author.with_context(lang="fr_FR").bio == "Bio française"
    assert author.with_context(lang="de_DE").bio == "English bio"  # fallback to default language
    assert env["ktest.author"].with_context(lang="fr_FR").search([("bio", "ilike", "française")]) == author
    fr = env.ref("core.country_fr").with_context(lang="fr_FR")
    assert env.ref("core.country_gb").with_context(lang="fr_FR").name == "Royaume-Uni"
    assert fr.name == "France"


def test_company_dependent_field(env):  # type: ignore[no-untyped-def]
    main = env.ref("core.main_company")
    eu = env.ref("core.demo_company_eu")
    author = env["ktest.author"].create({"name": "CD", "nickname": "Main nick"})
    assert author.nickname == "Main nick"
    author.with_company(eu).write({"nickname": "EU nick"})
    assert author.with_company(eu).nickname == "EU nick"
    assert author.with_company(main).nickname == "Main nick"


# -- archive, soft delete, copy -------------------------------------------------------------------


def test_archive_and_active_test(env):  # type: ignore[no-untyped-def]
    author = env["ktest.author"].create({"name": "Archive me"})
    author.action_archive()
    assert not env["ktest.author"].search([("name", "=", "Archive me")])
    assert env["ktest.author"].with_context(active_test=False).search([("name", "=", "Archive me")]) == author
    author.action_unarchive()
    assert env["ktest.author"].search([("name", "=", "Archive me")]) == author


def test_soft_delete_and_restore(env):  # type: ignore[no-untyped-def]
    book = env["ktest.book"].create({"name": "Trash me"})
    book.unlink()
    assert book.exists()  # row still there
    assert not env["ktest.book"].search([("name", "=", "Trash me")])
    trashed = env["ktest.book"].with_context(include_deleted=True).search([("name", "=", "Trash me")])
    assert trashed == book
    assert trashed.deleted_at is not None
    assert trashed.deleted_by.login == "admin"
    book.restore()
    assert env["ktest.book"].search([("name", "=", "Trash me")]) == book


def test_copy(env):  # type: ignore[no-untyped-def]
    tag = env["ktest.tag"].create({"name": f"copytag-{dt.datetime.now().timestamp()}"})
    book = env["ktest.book"].create(
        {"name": "Original", "tag_ids": [tag.ids[0]], "chapter_ids": [("create", {"name": "c1", "words": 5})]}
    )
    dup = book.copy()
    assert dup.name == "Original (copy)"
    assert dup.tag_ids == tag
    assert dup.chapter_ids.mapped("name") == ["c1"]
    assert dup.chapter_ids != book.chapter_ids
    assert dup.reference != book.reference


# -- search ----------------------------------------------------------------------------------------


def test_search_operators(env):  # type: ignore[no-untyped-def]
    books = env["ktest.book"]
    pride = env.ref("kernel_test_a.book_pride")
    assert pride in books.search([("name", "ilike", "pride")])
    assert pride in books.search([("name", "starts_with", "Pride")])
    assert pride in books.search([("page_count", ">", 400), ("page_count", "<=", 432)])
    assert pride in books.search([("published", "<", "1820-01-01"), ("published", "is_set")])
    assert pride in books.search([("status", "in", ["published", "review"])])
    assert pride not in books.search([("status", "not_in", ["published"])])
    assert pride in books.search([("author_id", "ilike", "austen")])
    assert pride in books.search([("author_id.country_id.code", "=", "GB")])
    assert pride in books.search([("tag_ids.name", "=", "Romance")])
    assert pride in books.search([("tag_ids", "any", [["name", "=", "Romance"]])])
    assert pride not in books.search([("tag_ids", "not_any", [["name", "=", "Romance"]])])
    assert pride in books.search([("chapter_ids.words", ">", 1400)])
    assert pride in books.search({"any": [["name", "=", "Pride and Prejudice"], ["name", "=", "nothing"]]})
    assert pride not in books.search({"not": ["name", "=", "Pride and Prejudice"]})
    assert env.ref("kernel_test_a.book_draft") in books.search([("tag_ids", "is_not_set")])
    assert pride in books.search([("display_name", "ilike", "prejudice")])
    assert books.search_count([("author_id", "=", env.ref("kernel_test_a.author_austen").ids[0])]) == 2


def test_search_order_limit_offset(env):  # type: ignore[no-untyped-def]
    books = env["ktest.book"]
    by_pages = books.search([("published", "is_set")], order="page_count desc")
    pages = by_pages.mapped("page_count")
    assert pages == sorted(pages, reverse=True)
    first_two = books.search([("published", "is_set")], order="page_count desc", limit=2)
    assert first_two.ids == by_pages.ids[:2]
    third = books.search([("published", "is_set")], order="page_count desc", limit=1, offset=2)
    assert third.ids == by_pages.ids[2:3]
    by_author = books.search([("published", "is_set")], order="author_id, name")
    assert by_author.mapped("author_id.name") == sorted(by_author.mapped("author_id.name"))
    with pytest.raises(ValidationError):
        books.search([], order="nope")


def test_child_of_parent_of(env):  # type: ignore[no-untyped-def]
    main = env.ref("core.main_company")
    eu = env.ref("core.demo_company_eu")
    companies = env["core.company"]
    assert set(companies.search([("id", "child_of", main.ids)]).ids) >= {main.ids[0], eu.ids[0]}
    assert companies.search([("id", "parent_of", eu.ids)]) == main | eu
    # admin's allowed companies are [main] only, so the multi-company rule hides marie; widen the scope
    users = env["core.user"].with_context(active_test=False, allowed_company_ids=[main.ids[0], eu.ids[0]])
    assert env.ref("core.user_demo_fr") in users.search([("company_id", "child_of", main.ids)])
    assert env.ref("core.user_demo_fr") not in env["core.user"].search([("company_id", "child_of", main.ids)])


def test_read_group(env):  # type: ignore[no-untyped-def]
    books = env["ktest.book"]
    groups = books.read_group(
        [("published", "is_set")],
        ["page_count:sum", "price:avg"],
        ["author_id"],
        orderby="page_count:sum desc",
    )
    assert groups[0]["author_id"]["display_name"] == "Jane Austen"  # 432 + 474 pages
    assert groups[0]["page_count:sum"] == 906
    assert groups[0]["__count"] == 2
    assert groups[0]["price:avg"] == pytest.approx(11.75)
    dumas = next(g for g in groups if g["author_id"]["display_name"] == "Alexandre Dumas")
    assert dumas["__count"] == 1
    assert dumas["page_count:sum"] == 700
    austen = groups[0]
    assert books.search(austen["__domain"]) == books.search(
        [("published", "is_set"), ("author_id", "=", austen["author_id"]["id"])]
    )
    by_year = books.read_group([], ["page_count:sum"], ["published:year"])
    years = [g["published:year"] for g in by_year]
    assert years[0] is None  # drafts without a date group first
    assert any(y and y.startswith("1813") for y in years)
    totals = books.read_group([], ["page_count:sum", "page_count:max"], [])
    assert totals[0]["__count"] == books.search_count([])


def test_name_search_and_display_name(env):  # type: ignore[no-untyped-def]
    results = env["ktest.author"].name_search("aust")
    assert [name for _, name in results] == ["Jane Austen"]
    chapter = env["ktest.chapter"].create({"name": "Ch", "book_id": env.ref("kernel_test_a.book_pride").ids[0]})
    assert chapter.display_name == "Ch"
    log = env["core.audit_log"].elevated().search([], limit=1)
    assert log.display_name  # rec_name fallback


# -- sequences, currency, references, metadata ----------------------------------------------------


def test_sequences_gapless_and_standard(env):  # type: ignore[no-untyped-def]
    seq = env["core.sequence"]
    year = dt.datetime.now(dt.UTC).strftime("%Y")
    a = seq.next_by_code("ktest.book")
    b = seq.next_by_code("ktest.book")
    assert a.startswith(f"BK/{year}/") and int(b.split("/")[-1]) == int(a.split("/")[-1]) + 1
    std = seq.create({"name": "std", "code": "ktest.std", "prefix": "S", "padding": 3, "implementation": "standard"})
    assert std.next_by_id() == "S001"
    assert std.next_by_id() == "S002"
    with pytest.raises(UserError):
        seq.next_by_code("does.not.exist")


def test_currency_conversion_and_rounding(env):  # type: ignore[no-untyped-def]
    usd, eur, jpy = env.ref("core.USD"), env.ref("core.EUR"), env.ref("core.JPY")
    assert usd.convert(100, eur, date=dt.date(2026, 6, 1)) == 92.0
    assert eur.convert(92, usd, date=dt.date(2026, 6, 1)) == 100.0
    assert jpy.round(1234.6) == 1235.0
    with pytest.raises(UserError):
        usd.convert(1, jpy, date=dt.date(2026, 6, 1))  # no JPY rate


def test_external_ids(env):  # type: ignore[no-untyped-def]
    pride = env.ref("kernel_test_a.book_pride")
    assert pride.get_external_id()[pride.ids[0]] == "kernel_test_a.book_pride"
    assert env.ref("nope.nope", raise_if_not_found=False) is None
    with pytest.raises(MissingError):
        env.ref("nope.nope")


def test_metadata_synced(env):  # type: ignore[no-untyped-def]
    model = env["core.model"].search([("name", "=", "ktest.book")])
    assert model.description == "Book"
    assert model.module == "kernel_test_a"
    fields = {f.name: f for f in model.field_ids}
    assert fields["isbn"].module == "kernel_test_b"
    assert fields["author_name"].related == "author_id.name"
    assert fields["total_words"].store is True and fields["total_words"].compute == "_compute_total_words"
    assert fields["tag_ids"].comodel == "ktest.tag"
    assert fields["status"].selection == [
        ["draft", "Draft"],
        ["review", "In review"],
        ["published", "Published"],
    ]
    assert env["ktest.book"].fields_get(["price"])["price"]["currency_field"] == "currency_id"


def test_schema_has_no_drift(env):  # type: ignore[no-untyped-def]
    from tessera.orm.schema import check_drift

    assert check_drift(env.cr, env.registry) == []


def test_sql_constraint_message(env):  # type: ignore[no-untyped-def]
    env["ktest.tag"].create({"name": "unique-tag-x"})
    with pytest.raises(ValidationError, match="Tag names must be unique"):
        env["ktest.tag"].create({"name": "unique-tag-x"})
