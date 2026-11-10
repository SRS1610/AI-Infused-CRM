"""Chatter: followers, tracking, posting, mentions, notifications, activities, reactions, access, inbound mail
(1.3-a … 1.3-d)."""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest

from modules.chatter import api as chatter_api
from modules.chatter.inbound import parse_email, process_email
from modules.chatter.thread import parse_reply_token, post_message, reply_address
from tessera.exceptions import AccessError, UserError

pytestmark = pytest.mark.db


def ctx_for(env: Any) -> Any:
    return SimpleNamespace(env=env, uid=env.uid, registry=env.registry, authenticated=True)


def _messages(env: Any, book: Any, **extra: Any) -> Any:
    domain = [("model", "=", "ktest.book"), ("res_id", "=", book.ids[0])]
    domain += [(k, "=", v) for k, v in extra.items()]
    return env["chatter.message"].elevated().search(domain, order="id")


def _followers(env: Any, book: Any) -> set[int]:
    found = env["chatter.follower"].elevated().search([("model", "=", "ktest.book"), ("res_id", "=", book.ids[0])])
    return {f.user_id.ids[0] for f in found}


@pytest.fixture
def users(env: Any, env_for: Any) -> SimpleNamespace:
    return SimpleNamespace(admin=env, librarian=env_for("librarian"), reader=env_for("reader"), demo=env_for("demo"))


@pytest.fixture
def book(users: SimpleNamespace) -> Any:
    return users.librarian["ktest.book"].create({"name": "Chatter Book"})


def test_create_subscribes_creator_and_logs(book: Any, users: SimpleNamespace) -> None:
    assert _followers(users.admin, book) == {users.librarian.uid}
    created = _messages(users.admin, book)
    assert len(created) == 1 and created.message_type == "notification" and "created" in created.body


def test_tracking_records_old_and_new_display_values(book: Any, users: SimpleNamespace) -> None:
    book.write({"status": "review", "user_id": users.demo.uid})
    tracking = _messages(users.admin, book, message_type="notification")[-1].tracking_value_ids
    by_field = {t.field_name: (t.old_display, t.new_display) for t in tracking}
    assert by_field["status"] == ("Draft", "In review")
    assert by_field["user_id"] == (None, "Dana Demo")
    assert users.demo.uid in _followers(users.admin, book)  # the new responsible follows
    book.write({"page_count": 10})  # untracked field: no new message
    assert len(_messages(users.admin, book, message_type="notification")) == 2


def test_tracking_can_be_disabled(book: Any, users: SimpleNamespace) -> None:
    book.with_context(tracking_disable=True).write({"status": "review"})
    assert len(_messages(users.admin, book, message_type="notification")) == 1


def test_comment_notifies_followers_and_mentions(book: Any, users: SimpleNamespace) -> None:
    book.write({"status": "published"})  # readers can only see published books
    book.message_subscribe([users.reader.uid, users.demo.uid])
    notes = users.admin["core.notification"].elevated()
    before = notes.search_count([])
    book.message_post("<p>Hello @admin, have a look</p>")
    created = notes.search([], order="id desc", limit=notes.search_count([]) - before)
    kinds = {(n.user_id.ids[0], n.kind) for n in created}
    assert (users.reader.uid, "message") in kinds and (users.admin.uid, "mention") in kinds
    assert all(n.user_id.ids[0] != users.librarian.uid for n in created)  # never the author
    assert all(n.user_id.ids[0] != users.demo.uid for n in created)  # demo cannot read books: not notified
    msg = _messages(users.admin, book, message_type="comment")
    assert msg.mention_user_ids.ids == [users.admin.uid]


def test_internal_note_not_sent_to_default_followers(book: Any, users: SimpleNamespace) -> None:
    book.message_subscribe([users.demo.uid])
    notes = users.admin["core.notification"].elevated()
    before = notes.search_count([("user_id", "=", users.demo.uid)])
    book.message_post("internal stuff", internal=True)
    assert notes.search_count([("user_id", "=", users.demo.uid)]) == before
    assert _messages(users.admin, book, message_type="note").is_internal


def test_subscribing_others_requires_write(book: Any, users: SimpleNamespace) -> None:
    book.write({"status": "published"})
    reader_book = users.reader["ktest.book"].browse(book.ids)
    reader_book.message_subscribe()  # self: read access is enough
    assert users.reader.uid in _followers(users.admin, book)
    with pytest.raises(AccessError):
        reader_book.message_subscribe([users.demo.uid])


def test_thread_access_follows_record_access(book: Any, users: SimpleNamespace) -> None:
    # readers only see published books (record rule), so the draft's thread is hidden from them
    with pytest.raises(AccessError):
        chatter_api.list_messages("ktest.book", book.ids[0], None, 30, ctx_for(users.reader))
    with pytest.raises(AccessError):
        post_message(users.reader, "ktest.book", book.ids[0], "sneaky", check_access=True)
    book.write({"status": "published"})
    items = chatter_api.list_messages("ktest.book", book.ids[0], None, 30, ctx_for(users.reader))["items"]
    assert items  # visible once published


def test_portal_users_never_see_internal_messages(book: Any, users: SimpleNamespace, env: Any) -> None:
    portal_user = env["core.user"].create(
        {"login": "portal-chatter", "name": "Pat Portal", "group_ids": [("set", [env.ref("core.group_portal").ids[0]])]}
    )
    assert portal_user.share
    book.write({"status": "published"})
    book.message_post("public reply")
    book.message_post("secret note", internal=True)
    portal_env = env.with_user(portal_user.ids[0])
    ctx = ctx_for(portal_env)
    # portal users have no model access to books in the test module; grant read for this transaction
    env["core.model_access"].create(
        {
            "name": "portal books",
            "model_name": "ktest.book",
            "group_id": env.ref("core.group_portal").ids[0],
            "perm_read": True,
        }
    )
    env["core.record_rule"].create(
        {
            "name": "portal books",
            "model_name": "ktest.book",
            "group_ids": [("set", [env.ref("core.group_portal").ids[0]])],
            "domain": [["status", "=", "published"]],
        }
    )
    bodies = [m["body"] for m in chatter_api.list_messages("ktest.book", book.ids[0], None, 30, ctx)["items"]]
    assert any("public reply" in b for b in bodies) and not any("secret note" in b for b in bodies)
    with pytest.raises(AccessError):
        post_message(portal_env, "ktest.book", book.ids[0], "note", message_type="note", check_access=True)


def test_list_paginates_newest_first(book: Any, users: SimpleNamespace) -> None:
    for i in range(5):
        book.message_post(f"m{i}")
    ctx = ctx_for(users.librarian)
    first = chatter_api.list_messages("ktest.book", book.ids[0], None, 3, ctx)
    assert first["has_more"] and "m4" in first["items"][0]["body"]
    rest = chatter_api.list_messages("ktest.book", book.ids[0], first["items"][-1]["id"], 30, ctx)
    assert not rest["has_more"] and len(first["items"]) + len(rest["items"]) == 6


def test_edit_and_delete_only_own_messages(book: Any, users: SimpleNamespace) -> None:
    mid = book.message_post("draft text")[0]
    edited = chatter_api.edit_message(mid, "final text", ctx_for(users.librarian))
    assert edited["body"] == "final text" and edited["editable"]
    book.write({"status": "published"})
    with pytest.raises(AccessError):
        chatter_api.delete_message(mid, ctx_for(users.reader))
    chatter_api.delete_message(mid, ctx_for(users.librarian))
    assert not users.admin["chatter.message"].elevated().browse([mid]).exists()


def test_reactions_toggle(book: Any, users: SimpleNamespace) -> None:
    mid = book.message_post("nice")[0]
    out = chatter_api.toggle_reaction(mid, chatter_api.ReactionIn(emoji="👍"), ctx_for(users.librarian))
    assert out["reactions"] == [{"emoji": "👍", "count": 1, "users": ["Lee Librarian"], "mine": True}]
    out = chatter_api.toggle_reaction(mid, chatter_api.ReactionIn(emoji="👍"), ctx_for(users.librarian))
    assert out["reactions"] == []


def test_activities_schedule_done_and_dashboard(book: Any, users: SimpleNamespace) -> None:
    yesterday = (dt.date.today() - dt.timedelta(days=2)).isoformat()
    act_ids = book.activity_schedule("chatter.activity_type_call", "Call the author", yesterday, users.demo.uid)
    act = users.demo["chatter.activity"].browse(act_ids)
    assert act.state == "overdue" and book.activity_state == "overdue"
    note = (
        users.admin["core.notification"]
        .elevated()
        .search([("user_id", "=", users.demo.uid), ("kind", "=", "activity")])
    )
    assert note and "Call the author" in note[0].title
    dash = chatter_api.my_activities(None, ctx_for(users.demo))
    assert dash["summary"] == [{"model": "ktest.book", "label": "Book", "overdue": 1, "today": 0, "planned": 0}]
    # the activity belongs to demo: the librarian sees it in the thread but not in their own list
    assert not users.librarian["chatter.activity"].search([("id", "in", act_ids)])
    assert [
        a["id"] for a in chatter_api.list_activities("ktest.book", book.ids[0], ctx_for(users.librarian))
    ] == act_ids
    act.action_snooze(5)
    assert act.state == "planned"
    act.action_done("Talked, all good")
    assert act.done and book.activity_state is None
    done_msg = _messages(users.admin, book, message_type="activity_done")
    assert "Call" in done_msg.body and "all good" in done_msg.body
    with pytest.raises(UserError):
        act.action_done()


def test_search_activities_by_state(book: Any, users: SimpleNamespace) -> None:
    today = dt.date.today().isoformat()
    book.activity_schedule(summary="today one", date_deadline=today)
    found = users.librarian["chatter.activity"].search([("state", "=", "today")])
    assert "today one" in found.mapped("summary")
    assert "today one" not in users.librarian["chatter.activity"].search([("state", "=", "overdue")]).mapped("summary")


def test_email_preference_queues_mail(book: Any, users: SimpleNamespace) -> None:
    users.admin["core.user"].browse([users.reader.uid]).write(
        {"notification_preference": "email", "email": "rita@example.test"}
    )
    book.write({"status": "published"})
    book.message_subscribe([users.reader.uid])
    book.message_post("<p>Ping by email</p>", subject="Status")
    mail = (
        users.admin["core.mail_queue"]
        .elevated()
        .search([("email_to", "=", "rita@example.test")], order="id desc", limit=1)
    )
    assert mail and mail.subject == "Status" and "Ping by email" in mail.body_html
    assert parse_reply_token(mail.reply_to.split("@")[0]) == ("ktest.book", book.ids[0])


def test_digest_job_sends_unread_once(book: Any, users: SimpleNamespace) -> None:
    from modules.chatter.models.fetch import digest_job

    users.admin["core.user"].browse([users.reader.uid]).write(
        {"notification_digest": True, "email": "rita@example.test"}
    )
    book.write({"status": "published"})
    book.message_subscribe([users.reader.uid])
    book.message_post("digest me")
    sys_env = users.admin.elevated()
    assert digest_job(sys_env)["digests"] >= 1
    mail = sys_env["core.mail_queue"].search([("email_to", "=", "rita@example.test")], order="id desc", limit=1)
    assert "digest" in mail.subject.lower() and "digest me" in mail.body_html
    pending = [("user_id", "=", users.reader.uid), ("digested", "=", False), ("is_read", "=", False)]
    assert not sys_env["core.notification"].search(pending)  # each notification goes in one digest only


def test_force_delete_removes_thread(book: Any, users: SimpleNamespace) -> None:
    book.message_post("bye")
    bid = book.ids[0]
    book.unlink()  # soft delete keeps the history
    assert users.admin["chatter.message"].elevated().search_count([("model", "=", "ktest.book"), ("res_id", "=", bid)])
    users.admin["ktest.book"].with_context(include_deleted=True).browse([bid]).with_context(force_delete=True).unlink()
    assert (
        not users.admin["chatter.message"].elevated().search_count([("model", "=", "ktest.book"), ("res_id", "=", bid)])
    )


# -- inbound email ---------------------------------------------------------------------------------

RAW = """From: Dana Demo <dana@example.com>
To: {to}
Subject: {subject}
Message-ID: <{mid}@mail.example.com>
{extra}MIME-Version: 1.0
Content-Type: multipart/mixed; boundary="b1"

--b1
Content-Type: text/plain; charset=utf-8

Hello team,

line two
--b1
Content-Type: text/plain; name="notes.txt"
Content-Disposition: attachment; filename="notes.txt"

attached notes
--b1--
"""


def _raw(to: str, subject: str = "Inbound", mid: str = "m1", extra: str = "") -> str:
    return RAW.format(to=to, subject=subject, mid=mid, extra=extra)


def test_parse_email() -> None:
    parsed = parse_email(_raw("books@tessera.local"))
    assert parsed["from"] == "dana@example.com" and parsed["subject"] == "Inbound"
    assert "<p>Hello team,</p>" in parsed["body"] and parsed["attachments"][0]["name"] == "notes.txt"


def test_alias_creates_record_and_reply_threads(users: SimpleNamespace) -> None:
    env = users.admin
    env["core.mail_alias"].create({"alias_name": "books", "model_name": "ktest.book", "defaults": {"page_count": 7}})
    result = process_email(env, _raw("Books <books@tessera.local>", "A new manuscript", "alias-1"))
    assert result["status"] == "created"
    book = env["ktest.book"].browse([result["res_id"]])
    assert book.name == "A new manuscript" and book.page_count == 7
    msg = env["chatter.message"].elevated().browse([result["message"]])
    assert (
        msg.message_type == "email" and msg.author_id.ids == [users.demo.uid] and msg.attachment_ids.name == "notes.txt"
    )
    # a reply to the reply+ address lands in the same thread
    reply = process_email(env, _raw(reply_address("ktest.book", book.ids[0]), "Re: manuscript", "reply-1"))
    assert reply == {"status": "posted", "model": "ktest.book", "res_id": book.ids[0], "message": reply["message"]}
    # a reply that only carries In-Reply-To also threads
    threaded = process_email(
        env, _raw("someone@else.test", "Re: Re", "reply-2", extra="In-Reply-To: <reply-1@mail.example.com>\n")
    )
    assert threaded["status"] == "posted" and threaded["res_id"] == book.ids[0]
    assert process_email(env, _raw("books@tessera.local", mid="alias-1"))["status"] == "duplicate"


def test_inbound_rejections(users: SimpleNamespace) -> None:
    env = users.admin
    assert process_email(env, _raw("nobody@tessera.local", mid="x1"))["status"] == "bounced"
    forged = reply_address("ktest.book", 1).replace("reply+", "reply+00")
    assert process_email(env, _raw(forged, mid="x2"))["status"] == "bounced"
    auto = process_email(env, _raw("books@tessera.local", mid="x3", extra="Auto-Submitted: auto-replied\n"))
    assert auto["status"] == "ignored"


def test_imap_fetch_processes_unseen(users: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    env = users.admin
    env["core.mail_alias"].create({"alias_name": "books", "model_name": "ktest.book"})
    server = env["chatter.fetch_server"].create(
        {"name": "inbox", "host": "imap.test", "username": "u", "password": "p"}
    )
    stored: list[tuple[bytes, str]] = []

    class FakeIMAP:
        def __init__(self, host: str, port: int) -> None:
            pass

        def login(self, user: str, password: str) -> None:
            assert password == "p"

        def select(self, folder: str) -> None:
            pass

        def search(self, charset: Any, criterion: str) -> tuple[str, list[bytes]]:
            return "OK", [b"1 2"]

        def fetch(self, num: bytes, what: str) -> tuple[str, list[Any]]:
            raw = _raw("books@tessera.local", f"via imap {num.decode()}", f"imap-{num.decode()}").encode()
            return "OK", [(b"1 (BODY[] {n}", raw), b")"]

        def store(self, num: bytes, op: str, flag: str) -> None:
            stored.append((num, flag))

        def logout(self) -> None:
            pass

    import modules.chatter.models.fetch as fetch_module

    monkeypatch.setattr(fetch_module.imaplib, "IMAP4_SSL", FakeIMAP)
    assert server.fetch() == {"processed": 2, "failed": 0}
    assert [n for n, _ in stored] == [b"1", b"2"]
    assert env["ktest.book"].search_count([("name", "like", "via imap")]) == 2
