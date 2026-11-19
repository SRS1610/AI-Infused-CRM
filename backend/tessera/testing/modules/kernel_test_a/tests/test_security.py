"""Access rights, record rules, field groups, multi-company, audit."""

from __future__ import annotations

import pytest

from tessera.exceptions import AccessDenied, AccessError, ValidationError

pytestmark = pytest.mark.db


def test_reader_acl_and_record_rules(env, env_for):  # type: ignore[no-untyped-def]
    reader = env_for("reader")
    books = reader["ktest.book"]
    visible = books.search([])
    assert set(visible.mapped("status")) == {"published"}
    draft = env.ref("kernel_test_a.book_draft")
    assert draft not in visible
    with pytest.raises(AccessError):
        _ = books.browse(draft.ids).name
    with pytest.raises(AccessError):
        books.create({"name": "nope"})
    pride = env.ref("kernel_test_a.book_pride")
    with pytest.raises(AccessError):
        books.browse(pride.ids).write({"name": "hacked"})
    assert books.search_count([("id", "in", [draft.ids[0], pride.ids[0]])]) == 1
    assert env.ref("kernel_test_a.book_pride").name == "Pride and Prejudice"  # admin unaffected


def test_librarian_full_access(env_for):  # type: ignore[no-untyped-def]
    lib = env_for("librarian")
    books = lib["ktest.book"]
    assert set(books.search([]).mapped("status")) >= {"draft", "published", "review"}
    book = books.create({"name": "Librarian book"})
    book.write({"status": "review"})
    book.with_context(force_delete=True).unlink()
    assert not book.exists()


def test_record_rule_variables_and_own_records(env, env_for):  # type: ignore[no-untyped-def]
    reader = env_for("reader")
    # readers may see their own drafts (create_uid = $uid) even if unpublished
    reader_user = env["core.user"].search([("login", "=", "reader")])
    own = env["ktest.book"].with_user(reader_user.ids[0]).elevated().create({"name": "Reader draft", "status": "draft"})
    env.cr.execute(
        "UPDATE ktest_book SET create_uid = :uid WHERE id = :id",
        {"uid": reader_user.ids[0], "id": own.ids[0]},
    )
    env.cache.invalidate()
    assert own in reader["ktest.book"].search([("name", "=", "Reader draft")])


def test_field_groups(env, env_for):  # type: ignore[no-untyped-def]
    author = env["ktest.author"].create({"name": "Secret keeper", "secret_notes": "classified"})
    env.cr.execute("UPDATE ktest_author SET secret_notes = 'classified' WHERE id = :id", {"id": author.ids[0]})
    reader = env_for("reader")
    rec = reader["ktest.author"].browse(author.ids)
    assert rec.name == "Secret keeper"
    with pytest.raises(AccessError):
        _ = rec.secret_notes
    assert "secret_notes" not in rec.fields_get()
    with pytest.raises(AccessError):
        rec.read(["secret_notes"])
    assert author.secret_notes == "classified"  # admin is in core.group_system


def test_multi_company_isolation(env, env_for):  # type: ignore[no-untyped-def]
    eu = env.ref("core.demo_company_eu")
    main = env.ref("core.main_company")
    eu_author = env["ktest.author"].with_company(eu).create({"name": "EU only"})
    assert eu_author.company_id == eu
    marie = env_for("marie")  # only allowed in the EU company
    assert eu_author in marie["ktest.author"].elevated().search([("name", "=", "EU only")])
    main_author = env["ktest.author"].with_company(main).create({"name": "Main only"})
    librarian = env_for("librarian")  # main company only
    assert main_author in librarian["ktest.author"].search([])
    assert eu_author not in librarian["ktest.author"].search([])
    with pytest.raises(AccessError):
        _ = librarian["ktest.author"].browse(eu_author.ids).name
    # the demo user has both companies allowed and selects both
    dana = env_for("demo").with_context(allowed_company_ids=[main.ids[0], eu.ids[0]])
    both = dana["ktest.author"].elevated().search([("name", "in", ["EU only", "Main only"])])
    assert len(both) == 2


def test_no_acl_means_no_access(env_for):  # type: ignore[no-untyped-def]
    reader = env_for("reader")
    with pytest.raises(AccessError):
        reader["core.model_access"].search([])
    assert reader["core.user"].search_count([]) >= 1  # readers can read users


def test_authentication(env):  # type: ignore[no-untyped-def]
    users = env["core.user"]
    user = users.authenticate("Librarian", "librarian1234")
    assert user.login == "librarian"
    assert user.last_login is not None
    with pytest.raises(AccessDenied):
        users.authenticate("librarian", "wrong")
    with pytest.raises(AccessDenied):
        users.authenticate("system", "anything")  # inactive
    with pytest.raises(ValidationError):
        user.set_password("short")
    user.set_password("newpassword123")
    assert users.authenticate("librarian", "newpassword123") == user
    assert user.has_group("kernel_test_a.group_reader")  # implied through librarian
    assert not user.has_group("core.group_system")


def test_elevated_bypasses_everything(env_for):  # type: ignore[no-untyped-def]
    reader = env_for("reader")
    assert reader["ktest.book"].elevated().search_count([]) > reader["ktest.book"].search_count([])
    assert reader["core.model_access"].elevated().search_count([]) > 0


def test_audit_log(env):  # type: ignore[no-untyped-def]
    book = env["ktest.book"].create({"name": "Audited", "page_count": 10})
    logs = env["core.audit_log"].elevated()
    created = logs.search([("model", "=", "ktest.book"), ("res_id", "=", book.ids[0]), ("operation", "=", "create")])
    assert len(created) == 1
    assert created.user_id.login == "admin"
    assert created.changes["name"]["new"] == "Audited"
    book.write({"page_count": 20, "name": "Audited"})
    written = logs.search([("model", "=", "ktest.book"), ("res_id", "=", book.ids[0]), ("operation", "=", "write")])
    assert len(written) == 1
    assert written.changes == {"page_count": {"old": 10, "new": 20}}
    book.unlink()
    deleted = logs.search([("model", "=", "ktest.book"), ("res_id", "=", book.ids[0]), ("operation", "=", "unlink")])
    assert len(deleted) == 1
    # chapters are not audited
    assert not logs.search([("model", "=", "ktest.chapter")])


def test_security_cache_invalidation(env, env_for):  # type: ignore[no-untyped-def]
    reader_user = env["core.user"].search([("login", "=", "reader")])
    reader = env_for("reader")
    with pytest.raises(AccessError):
        reader["ktest.book"].create({"name": "x"})
    reader_user.write({"group_ids": [("add", env.ref("kernel_test_a.group_librarian").ids[0])]})
    book = reader["ktest.book"].create({"name": "now allowed"})
    assert book.exists()
