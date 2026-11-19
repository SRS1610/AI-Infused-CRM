"""Chatter HTTP API end to end (committed transactions through the FastAPI app)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from tessera.config import get_settings, reset_settings_cache

pytestmark = pytest.mark.db


def _book(librarian, status: str = "published") -> int:  # type: ignore[no-untyped-def]
    resp = librarian.post(
        "/api/v1/models/ktest.book", json={"values": {"name": f"http-{uuid.uuid4().hex[:6]}", "status": status}}
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


def test_post_list_react_edit_delete(librarian, reader) -> None:  # type: ignore[no-untyped-def]
    bid = _book(librarian)
    base = f"/api/v1/chatter/ktest.book/{bid}"
    created = librarian.post(f"{base}/messages", json={"body": "<p>Hi <script>x</script>@reader</p>"})
    assert created.status_code == 201
    msg = created.json()
    assert "<script>" not in msg["body"] and msg["editable"]
    assert librarian.post(f"{base}/messages", json={"body": "  "}).status_code == 400
    listing = reader.get(f"{base}/messages").json()
    assert listing["items"][0]["id"] == msg["id"] and not listing["items"][0]["editable"]
    react = reader.post(f"/api/v1/chatter/messages/{msg['id']}/reactions", json={"emoji": "🎉"}).json()
    assert react["reactions"][0]["users"] == ["Rita Reader"]
    assert reader.patch(f"/api/v1/chatter/messages/{msg['id']}", json={"body": "hacked"}).status_code == 403
    assert librarian.patch(f"/api/v1/chatter/messages/{msg['id']}", json={"body": "edited"}).json()["body"] == "edited"
    assert librarian.delete(f"/api/v1/chatter/messages/{msg['id']}").status_code == 204
    # the mentioned reader got an in-app notification
    notes = reader.get("/api/v1/models/core.notification", params={"fields": "title,kind,res_id"}).json()["items"]
    assert any(n["kind"] == "mention" and n["res_id"] == bid for n in notes)


def test_hidden_record_thread_is_forbidden(librarian, reader) -> None:  # type: ignore[no-untyped-def]
    bid = _book(librarian, status="draft")
    resp = reader.get(f"/api/v1/chatter/ktest.book/{bid}/messages")
    assert resp.status_code in (403, 404)
    assert reader.post(f"/api/v1/chatter/ktest.book/{bid}/messages", json={"body": "x"}).status_code in (403, 404)
    assert reader.get("/api/v1/chatter/core.user/2/messages").status_code == 400  # not a thread model


def test_followers_and_activities(librarian, reader) -> None:  # type: ignore[no-untyped-def]
    bid = _book(librarian)
    base = f"/api/v1/chatter/ktest.book/{bid}"
    followed = reader.post(f"{base}/followers", json={}).json()
    assert followed["following"] and {s["name"] for s in followed["subtypes"]} >= {"Discussions"}
    assert not reader.delete(f"{base}/followers").json()["following"]
    reader_id = reader.payload["user"]["id"]
    assert reader.post(f"{base}/followers", json={"user_ids": [2]}).status_code == 403  # others need write
    due = (dt.date.today() + dt.timedelta(days=1)).isoformat()
    act = librarian.post(
        f"{base}/activities",
        json={
            "activity_type": "chatter.activity_type_call",
            "summary": "Call back",
            "date_deadline": due,
            "user_id": reader_id,
        },
    )
    assert act.status_code == 201 and act.json()["state"] == "planned"
    aid = act.json()["id"]
    mine = reader.get("/api/v1/chatter/activities/mine").json()
    assert any(i["id"] == aid for i in mine["items"]) and mine["summary"][0]["planned"] >= 1
    assert reader.get("/api/v1/chatter/activities/mine", params={"state": "overdue"}).json()["items"] == []
    snoozed = reader.post(f"/api/v1/chatter/activities/{aid}/snooze", json={"days": 2}).json()
    assert snoozed["date_deadline"] > due
    assert reader.post(f"/api/v1/chatter/activities/{aid}/done", json={"feedback": "done!"}).json() == {"ok": True}
    assert librarian.get(f"{base}/activities").json() == []
    second = librarian.post(f"{base}/activities", json={"summary": "temp"}).json()
    assert librarian.delete(f"/api/v1/chatter/activities/{second['id']}").status_code == 204
    users = librarian.get("/api/v1/chatter/mentions", params={"q": "rit"}).json()
    assert [u["login"] for u in users] == ["reader"]


def test_inbound_endpoint_requires_token_or_admin(client, admin, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    raw = b"From: x@example.test\r\nTo: nobody@tessera.local\r\nSubject: hi\r\nMessage-ID: <http-in@x>\r\n\r\nbody\r\n"
    assert client.post("/api/v1/chatter/inbound", content=raw).status_code == 401
    monkeypatch.setenv("TESSERA_MAIL_INBOUND_TOKEN", "inbound-secret")
    reset_settings_cache()
    try:
        assert get_settings().mail_inbound_token == "inbound-secret"
        bad = client.post("/api/v1/chatter/inbound", content=raw, headers={"X-Tessera-Inbound-Token": "nope"})
        assert bad.status_code == 401
        ok = client.post("/api/v1/chatter/inbound", content=raw, headers={"X-Tessera-Inbound-Token": "inbound-secret"})
        assert ok.status_code == 200 and ok.json()["status"] == "bounced"
    finally:
        monkeypatch.delenv("TESSERA_MAIL_INBOUND_TOKEN")
        reset_settings_cache()
    as_admin = admin.post("/api/v1/chatter/inbound", content=raw)
    assert as_admin.status_code == 200
