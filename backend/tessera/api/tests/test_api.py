"""HTTP API: authentication, REST, JSON-RPC, metadata, attachments, webhooks, web metadata."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response

from tessera.api.tests.conftest import Session

pytestmark = pytest.mark.db


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


# -- health & auth -----------------------------------------------------------------------------------


def test_health(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"]["registry"]["ready"] is True


def test_login_logout_and_csrf(client: TestClient) -> None:
    bad = client.post("/api/v1/auth/login", json={"login": "admin", "password": "nope"})
    assert bad.status_code == 401
    assert bad.json()["error"] == "AccessDenied"
    assert client.get("/api/v1/auth/session").status_code == 401
    session = Session(client, "ADMIN ", "admin1234")  # login is normalised
    assert session.payload["user"]["login"] == "admin"
    assert session.payload["user"]["is_admin"] is True
    assert "core.group_system" in session.payload["groups"]
    info = client.get("/api/v1/auth/session")
    assert info.status_code == 200 and info.json()["auth"] == "session"
    # unsafe request without CSRF token
    denied = client.post("/api/v1/models/ktest.tag", json={"values": {"name": _uniq("csrf")}})
    assert denied.status_code == 403 and denied.json().get("csrf") is True
    ok = session.post("/api/v1/models/ktest.tag", json={"values": {"name": _uniq("csrf")}})
    assert ok.status_code == 201
    assert session.post("/api/v1/auth/logout").json()["ok"] is True
    assert client.get("/api/v1/auth/session").status_code == 401


def test_login_rate_limit(client: TestClient) -> None:
    for _ in range(10):
        client.post("/api/v1/auth/login", json={"login": "nobody-here", "password": "wrong"})
    assert client.post("/api/v1/auth/login", json={"login": "nobody-here", "password": "wrong"}).status_code == 429


def test_password_change_revokes_other_sessions(client: TestClient, app) -> None:  # type: ignore[no-untyped-def]
    other = TestClient(app, base_url="http://localhost")
    Session(other, "librarian", "librarian1234")
    me = Session(client, "librarian", "librarian1234")
    wrong = me.post("/api/v1/auth/password", json={"current_password": "x", "new_password": "newpassword123"})
    assert wrong.status_code == 401
    assert (
        me.post(
            "/api/v1/auth/password", json={"current_password": "librarian1234", "new_password": "newpassword123"}
        ).status_code
        == 200
    )
    assert other.get("/api/v1/auth/session").status_code == 401
    assert client.get("/api/v1/auth/session").status_code == 200
    # restore for the other tests
    me.post("/api/v1/auth/password", json={"current_password": "newpassword123", "new_password": "librarian1234"})


def test_totp_enrolment_and_login(client: TestClient, app) -> None:  # type: ignore[no-untyped-def]
    import pyotp

    me = Session(client, "reader", "reader1234")
    setup = me.post("/api/v1/auth/totp/setup").json()
    assert setup["uri"].startswith("otpauth://totp/")
    assert me.post("/api/v1/auth/totp/enable", json={"code": "000000"}).status_code == 400
    code = pyotp.TOTP(setup["secret"]).now()
    assert me.post("/api/v1/auth/totp/enable", json={"code": code}).json()["ok"] is True
    fresh = TestClient(app, base_url="http://localhost")
    step1 = fresh.post("/api/v1/auth/login", json={"login": "reader", "password": "reader1234"}).json()
    assert step1["totp_required"] is True
    bad = fresh.post("/api/v1/auth/totp", json={"pre_auth_token": step1["pre_auth_token"], "code": "123456"})
    assert bad.status_code == 401
    step1 = fresh.post("/api/v1/auth/login", json={"login": "reader", "password": "reader1234"}).json()
    good = fresh.post(
        "/api/v1/auth/totp", json={"pre_auth_token": step1["pre_auth_token"], "code": pyotp.TOTP(setup["secret"]).now()}
    )
    assert good.status_code == 200 and good.json()["user"]["totp_enabled"] is True
    # disable again
    me2 = Session(client, "reader", "reader1234") if False else None  # noqa: F841 - login would need TOTP now
    assert (
        fresh.post(
            "/api/v1/auth/totp/disable",
            json={"code": pyotp.TOTP(setup["secret"]).now()},
            headers={"X-CSRF-Token": good.json()["csrf_token"]},
        ).json()["ok"]
        is True
    )


# -- REST -----------------------------------------------------------------------------------------------


def test_rest_crud_and_etag(client: TestClient, admin: Session) -> None:
    created = admin.post(
        "/api/v1/models/ktest.book",
        json={"values": {"name": _uniq("REST"), "page_count": 10}, "fields": ["name", "page_count", "reference"]},
    )
    assert created.status_code == 201
    book = created.json()
    assert book["reference"].startswith("BK/")
    got = admin.get(f"/api/v1/models/ktest.book/{book['id']}", params={"fields": "name,author_id,tag_ids,price"})
    assert got.status_code == 200 and got.headers.get("ETag")
    etag = got.headers["ETag"]
    assert got.json()["author_id"] is None and got.json()["tag_ids"] == []
    stale = admin.patch(
        f"/api/v1/models/ktest.book/{book['id']}", json={"values": {"page_count": 11}}, headers={"If-Match": '"stale"'}
    )
    assert stale.status_code == 409
    updated = admin.patch(
        f"/api/v1/models/ktest.book/{book['id']}",
        json={"values": {"page_count": 11}, "fields": ["page_count"]},
        headers={"If-Match": etag},
    )
    assert updated.status_code == 200 and updated.json()["page_count"] == 11
    listed = admin.get(
        "/api/v1/models/ktest.book", params={"domain": json.dumps([["id", "=", book["id"]]]), "fields": "name"}
    )
    assert listed.json()["total"] == 1 and listed.json()["items"][0]["name"] == book["name"]
    bad_domain = admin.get("/api/v1/models/ktest.book", params={"domain": "{not json"})
    assert bad_domain.status_code == 422
    assert admin.get("/api/v1/models/nope.model").status_code == 404
    assert admin.get("/api/v1/models/ktest.book/999999").status_code == 404
    deleted = admin.delete(f"/api/v1/models/ktest.book/{book['id']}")
    assert deleted.status_code == 204
    assert admin.get(f"/api/v1/models/ktest.book/{book['id']}").status_code == 404


def test_rest_x2many_commands_and_read_group(admin: Session) -> None:
    tag = admin.post("/api/v1/models/ktest.tag", json={"values": {"name": _uniq("tag")}}).json()
    book = admin.post(
        "/api/v1/models/ktest.book",
        json={
            "values": {
                "name": _uniq("cmd"),
                "tag_ids": [["set", [tag["id"]]]],
                "chapter_ids": [["create", {"name": "c1", "words": 42}]],
            },
            "fields": ["tag_ids", "total_words"],
        },
    ).json()
    assert book["tag_ids"] == [tag["id"]] and book["total_words"] == 42
    groups = admin.post(
        "/api/v1/models/ktest.book/read_group",
        json={"domain": [["id", "=", book["id"]]], "aggregates": ["total_words:sum"], "groupby": ["status"]},
    ).json()["groups"]
    assert groups[0]["status"] == "draft" and groups[0]["total_words:sum"] == 42


def test_rest_permissions(client: TestClient, reader: Session) -> None:
    books = reader.get("/api/v1/models/ktest.book", params={"fields": "name,status"}).json()
    assert books["total"] >= 1 and {b["status"] for b in books["items"]} == {"published"}
    forbidden = reader.post("/api/v1/models/ktest.book", json={"values": {"name": "x"}})
    assert forbidden.status_code == 403 and forbidden.json()["error"] == "AccessError"
    assert reader.get("/api/v1/models/core.model_access").status_code == 403
    assert reader.get("/api/v1/models/ktest.author", params={"fields": "secret_notes"}).status_code == 403
    fields = reader.get("/api/v1/models/ktest.author/fields").json()["fields"]
    assert "secret_notes" not in fields and "name" in fields


def test_rest_method_calls(admin: Session) -> None:
    book = admin.post("/api/v1/models/ktest.book", json={"values": {"name": _uniq("pub")}}).json()
    res = admin.post(f"/api/v1/models/ktest.book/{book['id']}/call/action_publish", json={})
    assert res.status_code == 200 and res.json()["result"] is True
    assert (
        admin.get(f"/api/v1/models/ktest.book/{book['id']}", params={"fields": "status"}).json()["status"]
        == "published"
    )
    hidden = admin.post(f"/api/v1/models/ktest.book/{book['id']}/call/write", json={"args": [{"name": "hack"}]})
    assert hidden.status_code == 403
    private = admin.post(f"/api/v1/models/ktest.book/{book['id']}/call/_compute_title_upper", json={})
    assert private.status_code == 403
    seq = admin.post("/api/v1/models/core.sequence/call/next_by_code", json={"args": ["ktest.book"]})
    assert seq.status_code == 403  # next_by_code is not exposed


def test_name_search_and_metadata(admin: Session) -> None:
    hits = admin.get("/api/v1/models/ktest.author/name_search", params={"q": "aust"}).json()
    assert [h["display_name"] for h in hits] == ["Jane Austen"]
    models = admin.get("/api/v1/metadata/models").json()
    assert any(m["name"] == "ktest.book" for m in models)
    desc = admin.get("/api/v1/metadata/models/ktest.book").json()
    assert desc["fields"]["price"]["currency_field"] == "currency_id"
    assert "action_publish" in desc["methods"] and desc["access"]["unlink"] is True


# -- JSON-RPC ------------------------------------------------------------------------------------------


def test_jsonrpc_batch(admin: Session) -> None:
    name = _uniq("rpc")
    batch = [
        {"jsonrpc": "2.0", "id": 1, "method": "create", "params": {"model": "ktest.tag", "values": {"name": name}}},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "search_read",
            "params": {"model": "ktest.tag", "domain": [["name", "=", name]], "fields": ["name"]},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "nope", "params": {"model": "ktest.tag"}},
        {"jsonrpc": "2.0", "id": 4, "method": "create", "params": {"model": "ktest.tag", "values": {"name": name}}},
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "search_count",
            "params": {"model": "ktest.tag", "domain": [["name", "=", name]]},
        },
    ]
    resp = admin.post("/api/v1/jsonrpc", json=batch)
    assert resp.status_code == 200
    results = {r["id"]: r for r in resp.json()}
    assert isinstance(results[1]["result"], int)
    assert results[2]["result"][0]["name"] == name
    assert results[3]["error"]["code"] == -32601
    assert "unique" in results[4]["error"]["message"].lower()
    assert results[5]["result"] == 1  # the failed create was rolled back to its savepoint
    bad = admin.post("/api/v1/jsonrpc", content=b"{not json", headers={"Content-Type": "application/json"})
    assert bad.json()["error"]["code"] == -32700


# -- API keys ----------------------------------------------------------------------------------------------


def test_api_keys(client: TestClient, librarian: Session, app) -> None:  # type: ignore[no-untyped-def]
    created = librarian.post("/api/v1/auth/api-keys", json={"name": "ci"}).json()
    assert created["key"].startswith("tsk_") and created["prefix"] == created["key"][:12]
    anon = TestClient(app, base_url="http://localhost")
    info = anon.get("/api/v1/auth/session", headers={"Authorization": f"Bearer {created['key']}"})
    assert info.status_code == 200 and info.json()["auth"] == "api_key" and info.json()["user"]["login"] == "librarian"
    # API-key requests need no CSRF token and run as the key's user
    made = anon.post(
        "/api/v1/models/ktest.book",
        json={"values": {"name": _uniq("key")}},
        headers={"Authorization": f"Bearer {created['key']}"},
    )
    assert made.status_code == 201
    keys = librarian.get("/api/v1/auth/api-keys").json()
    assert any(k["id"] == created["id"] for k in keys)
    assert librarian.delete(f"/api/v1/auth/api-keys/{created['id']}").json()["ok"] is True
    assert anon.get("/api/v1/auth/session", headers={"Authorization": f"Bearer {created['key']}"}).status_code == 401
    assert anon.get("/api/v1/auth/session", headers={"Authorization": "Bearer tsk_garbage"}).status_code == 401


# -- attachments ------------------------------------------------------------------------------------------------


def test_attachments_upload_download(client: TestClient, admin: Session, app) -> None:  # type: ignore[no-untyped-def]
    pride = admin.get(
        "/api/v1/models/ktest.book",
        params={"domain": json.dumps([["name", "=", "Pride and Prejudice"]]), "fields": "id"},
    ).json()["items"][0]
    up = admin.post(
        "/api/v1/attachments",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
        data={"model": "ktest.book", "res_id": str(pride["id"])},
    )
    assert up.status_code == 201
    att = up.json()
    assert att["file_size"] == 11 and att["checksum"] == hashlib.sha256(b"hello world").hexdigest()
    down = admin.get(f"/api/v1/attachments/{att['id']}/content")
    assert (
        down.status_code == 200
        and down.content == b"hello world"
        and down.headers["content-type"].startswith("text/plain")
    )
    anon = TestClient(app, base_url="http://localhost")
    assert anon.get(f"/api/v1/attachments/{att['id']}/content").status_code == 403
    token = admin.post(f"/api/v1/models/core.attachment/{att['id']}/call/generate_access_token", json={}).json()[
        "result"
    ]
    assert anon.get(f"/api/v1/attachments/{att['id']}/content", params={"token": token}).content == b"hello world"
    assert anon.get(f"/api/v1/attachments/{att['id']}/content", params={"token": "bad"}).status_code == 403


# -- webhooks --------------------------------------------------------------------------------------------------


@respx.mock
def test_webhook_delivery_with_signature(admin: Session) -> None:
    route = respx.post("https://hooks.example.test/tags").mock(return_value=Response(200, text="ok"))
    hook = admin.post(
        "/api/v1/models/core.webhook",
        json={
            "values": {
                "name": "tags",
                "model_name": "ktest.tag",
                "events": ["create"],
                "url": "https://hooks.example.test/tags",
                "secret": "s3cret",
                "fields_sent": ["name"],
            }
        },
    ).json()
    name = _uniq("hooked")
    tag = admin.post("/api/v1/models/ktest.tag", json={"values": {"name": name}}).json()
    assert not route.called  # delivery is a background job, dispatched after commit
    from tessera.config import get_settings
    from tessera.jobs import run_now

    from tessera.orm.environment import SYSTEM_UID, environment_for

    url = get_settings().database_url
    with environment_for(url, SYSTEM_UID, su=True) as sys_env:
        queued = sys_env["core.job"].search([("name", "=", "core.webhook_deliver"), ("state", "=", "queued")]).ids
    assert queued and set(run_now(url, queued).values()) == {"done"}
    assert route.called
    request = route.calls.last.request
    body = json.loads(request.content)
    assert (
        body["event"] == "create"
        and body["model"] == "ktest.tag"
        and body["id"] == tag["id"]
        and body["data"]["name"] == name
    )
    expected = "sha256=" + hmac.new(b"s3cret", request.content, hashlib.sha256).hexdigest()
    assert request.headers["X-Tessera-Signature"] == expected
    deliveries = admin.get(
        "/api/v1/models/core.webhook_delivery",
        params={"domain": json.dumps([["webhook_id", "=", hook["id"]]]), "fields": "status,response_status,event"},
    ).json()["items"]
    assert deliveries and deliveries[0]["status"] == "sent" and deliveries[0]["response_status"] == 200
    # secret is stored encrypted and hidden from non-admin reads
    stored = admin.get(f"/api/v1/models/core.webhook/{hook['id']}", params={"fields": "secret"}).json()["secret"]
    assert stored.startswith("enc:v1:")
    admin.delete(f"/api/v1/models/core.webhook/{hook['id']}")


# -- web metadata ------------------------------------------------------------------------------------------------


def test_web_menus_actions_views(admin: Session, reader: Session) -> None:
    menus = admin.get("/api/v1/web/menus").json()
    roots = {m["name"]: m for m in menus}
    assert "Settings" in roots and "My account" in roots
    technical = next(c for c in roots["Settings"]["children"] if c["name"] == "Technical")
    assert any(c["action"] == "web.action_models" for c in technical["children"])
    reader_menus = reader.get("/api/v1/web/menus").json()
    assert "Settings" not in {m["name"] for m in reader_menus}
    assert "My account" in {m["name"] for m in reader_menus}
    action = admin.get("/api/v1/web/actions/web.action_users").json()
    assert action["model"] == "core.user" and action["view_types"] == ["list", "form"]
    assert reader.get("/api/v1/web/actions/web.action_users").status_code == 404
    views = admin.get("/api/v1/web/views", params={"model": "core.user", "types": "form,list,search,kanban"}).json()
    assert views["views"]["form"]["root"]["kind"] == "sheet"
    assert views["views"]["list"]["columns"][0]["field"] == "name"
    assert views["views"]["kanban"]["generated"] is True
    assert views["model"]["access"]["read"] is True
    # generated views for a model without declared views
    gen = admin.get(
        "/api/v1/web/views", params={"model": "ktest.book", "types": "form,list,search,calendar,pivot"}
    ).json()["views"]
    assert gen["form"]["generated"] and any(n.get("name") == "name" for n in gen["form"]["root"]["children"])
    assert gen["calendar"]["date_start"] == "published"
    assert "page_count" in gen["pivot"]["measures"]
    # field groups are stripped for the reader
    reader_views = reader.get("/api/v1/web/views", params={"model": "ktest.author", "types": "form"}).json()["views"][
        "form"
    ]
    assert "secret_notes" not in json.dumps(reader_views)
    assert "secret_notes" in json.dumps(
        admin.get("/api/v1/web/views", params={"model": "ktest.author", "types": "form"}).json()["views"]["form"]
    )


def test_view_inheritance_operations(env) -> None:  # type: ignore[no-untyped-def]
    from modules.web.views import apply_operations, resolve_view

    base = {
        "root": {
            "kind": "sheet",
            "children": [
                {"kind": "field", "name": "name"},
                {
                    "kind": "group",
                    "name": "main",
                    "children": [{"kind": "field", "name": "login"}, {"kind": "field", "name": "email"}],
                },
                {"kind": "notebook", "pages": [{"kind": "page", "name": "page_access", "children": []}]},
            ],
        }
    }
    ops = [
        {"op": "insert_after", "anchor": {"field": "login"}, "node": {"kind": "field", "name": "tz"}},
        {"op": "remove", "anchor": {"field": "email"}},
        {"op": "append", "anchor": {"name": "page_access"}, "node": {"kind": "field", "name": "group_ids"}},
        {"op": "set_attr", "anchor": {"field": "name"}, "attrs": {"readonly": True}},
        {"op": "prepend", "anchor": {"path": "/root"}, "node": {"kind": "label", "text": "User"}},
        {"op": "replace", "anchor": {"field": "tz"}, "node": {"kind": "field", "name": "lang"}},
    ]
    result = apply_operations(base, ops)
    group = result["root"]["children"][2]
    assert [n["name"] for n in group["children"]] == ["login", "lang"]
    assert result["root"]["children"][1]["readonly"] is True
    assert result["root"]["children"][0]["kind"] == "label"
    assert result["root"]["children"][3]["pages"][0]["children"][0]["name"] == "group_ids"
    with pytest.raises(Exception, match="not found"):
        apply_operations(base, [{"op": "remove", "anchor": {"field": "ghost"}}])

    # extension view stored in the database applies on resolution
    user_form = env.ref("web.view_user_form")
    ext = env["web.view"].create(
        {
            "name": "ext",
            "model_name": "core.user",
            "type": "form",
            "inherit_id": user_form.ids[0],
            "operations": [
                {"op": "insert_after", "anchor": {"field": "email"}, "node": {"kind": "field", "name": "signature"}}
            ],
        }
    )
    resolved = resolve_view(env, "core.user", "form")
    main = next(n for n in resolved["root"]["children"] if n.get("name") == "main")
    names = [n["name"] for n in main["children"]]
    assert names.index("signature") == names.index("email") + 1
    ext.unlink()
