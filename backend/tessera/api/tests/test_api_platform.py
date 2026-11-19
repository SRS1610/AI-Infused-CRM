"""1.2 acceptance coverage: lockout, recovery codes, OIDC, realtime, keyset/expand, view schema,
webhook retries, non-admin edits of other users."""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.parse
import uuid
from pathlib import Path

import pyotp
import pytest
import respx
from authlib.jose import JsonWebKey, JsonWebToken
from fastapi.testclient import TestClient
from httpx import Response

from tessera.api.tests.conftest import Session
from tessera.exceptions import ModuleError, ValidationError

pytestmark = pytest.mark.db


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _new_user(admin: Session, password: str = "password1234", **extra: object) -> dict[str, object]:
    login = _uniq("user").lower()
    groups = admin.get(
        "/api/v1/models/core.module_record",
        params={
            "domain": json.dumps([["module", "=", "kernel_test_a"], ["name", "=", "group_reader"]]),
            "fields": "res_id",
        },
    ).json()["items"]
    vals = {"login": login, "name": login, "password": password, "group_ids": [["set", [groups[0]["res_id"]]]], **extra}
    created = admin.post("/api/v1/models/core.user", json={"values": vals, "fields": ["login"]})
    assert created.status_code == 201, created.text
    return {"id": created.json()["id"], "login": login, "password": password}


# -- lockout ------------------------------------------------------------------------------------------------


def test_account_lockout_after_five_failures(app, admin: Session) -> None:  # type: ignore[no-untyped-def]
    user = _new_user(admin)
    client = TestClient(app, base_url="http://localhost")
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"login": user["login"], "password": "wrong"}).status_code == 401
    locked = client.post("/api/v1/auth/login", json={"login": user["login"], "password": user["password"]})
    assert locked.status_code == 401 and locked.json().get("locked") is True
    # an administrator unlocks the account
    from tessera.orm.environment import environment_for

    with environment_for(uid=1, su=True) as env:
        env["core.user"].browse([user["id"]]).unlock()
    assert (
        client.post("/api/v1/auth/login", json={"login": user["login"], "password": user["password"]}).status_code
        == 200
    )


# -- TOTP recovery codes ----------------------------------------------------------------------------------


def test_totp_recovery_codes(app, admin: Session) -> None:  # type: ignore[no-untyped-def]
    user = _new_user(admin)
    me = Session(TestClient(app, base_url="http://localhost"), str(user["login"]), str(user["password"]))
    secret = me.post("/api/v1/auth/totp/setup").json()["secret"]
    enabled = me.post("/api/v1/auth/totp/enable", json={"code": pyotp.TOTP(secret).now()}).json()
    codes = enabled["recovery_codes"]
    assert len(codes) == 10 and len(set(codes)) == 10
    client = TestClient(app, base_url="http://localhost")
    resp = client.post("/api/v1/auth/login", json={"login": user["login"], "password": user["password"]})
    assert resp.status_code == 200, resp.text
    step = resp.json()
    assert "pre_auth_token" in step, step
    ok = client.post("/api/v1/auth/totp", json={"pre_auth_token": step["pre_auth_token"], "code": codes[0]})
    assert ok.status_code == 200
    # a recovery code works once
    step = client.post("/api/v1/auth/login", json={"login": user["login"], "password": user["password"]}).json()
    again = client.post("/api/v1/auth/totp", json={"pre_auth_token": step["pre_auth_token"], "code": codes[0]})
    assert again.status_code == 401


# -- OIDC -------------------------------------------------------------------------------------------------------

ISSUER = "https://idp.example.test"


@respx.mock
def test_oidc_login_provisions_user(app, admin: Session) -> None:  # type: ignore[no-untyped-def]
    key = JsonWebKey.generate_key("RSA", 2048, is_private=True, options={"kid": "test-key"})
    public_jwks = {"keys": [key.as_dict(is_private=False)]}
    provider = admin.post(
        "/api/v1/models/core.sso_provider",
        json={
            "values": {
                "name": "Test IdP",
                "code": _uniq("idp"),
                "issuer": ISSUER,
                "client_id": "tessera",
                "client_secret": "s3cret",
                "auto_create_users": True,
            }
        },
    ).json()
    code = admin.get(
        f"/api/v1/models/core.sso_provider/{provider['id']}", params={"fields": "code,client_secret"}
    ).json()
    assert code["client_secret"].startswith("enc:v1:")
    respx.get(f"{ISSUER}/.well-known/openid-configuration").mock(
        return_value=Response(
            200,
            json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks",
                "id_token_signing_alg_values_supported": ["RS256"],
            },
        )
    )
    respx.get(f"{ISSUER}/jwks").mock(return_value=Response(200, json=public_jwks))
    client = TestClient(app, base_url="http://localhost")
    start = client.get(f"/api/v1/auth/oidc/{code['code']}/start", follow_redirects=False)
    assert start.status_code == 302
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(start.headers["location"]).query))
    assert query["code_challenge_method"] == "S256" and query["client_id"] == "tessera"
    email = f"{_uniq('sso')}@example.test"
    now = int(time.time())
    id_token = (
        JsonWebToken(["RS256"])
        .encode(
            {"alg": "RS256", "kid": "test-key"},
            {
                "iss": ISSUER,
                "aud": "tessera",
                "sub": _uniq("sub"),
                "email": email,
                "email_verified": True,
                "name": "SSO Person",
                "nonce": query["nonce"],
                "iat": now,
                "exp": now + 300,
            },
            key,
        )
        .decode()
    )
    token_route = respx.post(f"{ISSUER}/token").mock(
        return_value=Response(200, json={"id_token": id_token, "access_token": "at"})
    )
    bad_state = client.get(
        f"/api/v1/auth/oidc/{code['code']}/callback", params={"code": "c", "state": "wrong"}, follow_redirects=False
    )
    assert bad_state.status_code == 401
    done = client.get(
        f"/api/v1/auth/oidc/{code['code']}/callback",
        params={"code": "authcode", "state": query["state"]},
        follow_redirects=False,
    )
    assert done.status_code == 302, done.text
    assert b"code_verifier" in token_route.calls.last.request.content
    session = client.get("/api/v1/auth/session").json()
    assert session["user"]["login"] == email and session["auth"] == "session"


# -- realtime -----------------------------------------------------------------------------------------------------


def _read_until_pong(ws) -> list[dict[str, object]]:  # type: ignore[no-untyped-def]
    ws.send_json({"op": "ping"})
    messages = []
    for _ in range(50):
        msg = ws.receive_json()
        if msg.get("op") == "pong":
            return messages
        messages.append(msg)
    raise AssertionError("no pong")


def test_realtime_record_updates(app, admin: Session, librarian: Session) -> None:  # type: ignore[no-untyped-def]
    tag = admin.post("/api/v1/models/ktest.tag", json={"values": {"name": _uniq("rt")}}).json()
    with admin.client.websocket_connect("ws://localhost/api/v1/ws") as ws:
        ws.send_json(
            {"op": "subscribe", "channels": [f"record:ktest.tag:{tag['id']}", "model:ktest.tag", "user:999999"]}
        )
        subscribed = ws.receive_json()
        assert subscribed["op"] == "subscribed"
        assert set(subscribed["channels"]) == {f"record:ktest.tag:{tag['id']}", "model:ktest.tag"}
        assert subscribed["denied"] == ["user:999999"]
        assert (
            librarian.patch(f"/api/v1/models/ktest.tag/{tag['id']}", json={"values": {"color": 7}}).status_code == 200
        )
        events = _read_until_pong(ws)
        record_events = [e for e in events if e.get("channel") == f"record:ktest.tag:{tag['id']}"]
        assert record_events and record_events[0]["event"] == "write"
        assert record_events[0]["data"]["fields"] == ["color"]
        # rolled-back work publishes nothing
        failing = librarian.patch(f"/api/v1/models/ktest.tag/{tag['id']}", json={"values": {"nope": 1}})
        assert failing.status_code == 422
        assert not [e for e in _read_until_pong(ws) if e.get("channel")]


def test_realtime_denies_unreadable_records(app, reader: Session, admin: Session) -> None:  # type: ignore[no-untyped-def]
    draft = admin.get(
        "/api/v1/models/ktest.book",
        params={"domain": json.dumps([["status", "=", "draft"]]), "fields": "id", "limit": 1},
    ).json()["items"][0]
    with reader.client.websocket_connect("ws://localhost/api/v1/ws") as ws:
        ws.send_json({"op": "subscribe", "channels": [f"record:ktest.book:{draft['id']}", "model:core.model_access"]})
        reply = ws.receive_json()
        assert reply["channels"] == [] and len(reply["denied"]) == 2
    anon = TestClient(app, base_url="http://localhost")
    with pytest.raises(Exception):  # noqa: B017 - the server closes with 4401
        with anon.websocket_connect("ws://localhost/api/v1/ws") as ws:
            ws.receive_json()


# -- keyset pagination, expand -----------------------------------------------------------------------------


def test_keyset_pagination_and_expand(admin: Session) -> None:
    prefix = _uniq("page")
    admin.post("/api/v1/models/ktest.tag", json={"values": [{"name": f"{prefix}-{i}"} for i in range(5)]})
    domain = json.dumps([["name", "starts_with", prefix]])
    first = admin.get(
        "/api/v1/models/ktest.tag", params={"domain": domain, "after": 0, "limit": 2, "fields": "name"}
    ).json()
    assert len(first["items"]) == 2 and first["next_cursor"] == first["items"][-1]["id"]
    second = admin.get(
        "/api/v1/models/ktest.tag",
        params={"domain": domain, "after": first["next_cursor"], "limit": 2, "fields": "name"},
    ).json()
    third = admin.get(
        "/api/v1/models/ktest.tag",
        params={"domain": domain, "after": second["next_cursor"], "limit": 2, "fields": "name"},
    ).json()
    names = [i["name"] for i in first["items"] + second["items"] + third["items"]]
    assert names == [f"{prefix}-{i}" for i in range(5)] and third["next_cursor"] is None and first["total"] == 5
    bad = admin.get("/api/v1/models/ktest.tag", params={"after": 0, "order": "name"})
    assert bad.status_code == 422
    expanded = admin.get(
        "/api/v1/models/ktest.book",
        params={
            "domain": json.dumps([["name", "=", "Pride and Prejudice"]]),
            "fields": "name,author_id",
            "expand": "author_id",
        },
    ).json()["items"][0]
    assert expanded["author_id"]["name"] == "Jane Austen" and "country_id" in expanded["author_id"]
    assert admin.get("/api/v1/models/ktest.book", params={"expand": "tag_ids"}).status_code == 422


# -- authorisation of user records (1.2-d) ------------------------------------------------------------------


def test_non_admin_cannot_edit_other_users(admin: Session, librarian: Session) -> None:
    other = _new_user(admin)
    audit_domain = json.dumps([["model", "=", "core.user"], ["res_id", "=", other["id"]]])
    before = admin.get("/api/v1/models/core.audit_log", params={"domain": audit_domain, "fields": "id"}).json()["total"]
    denied = librarian.patch(f"/api/v1/models/core.user/{other['id']}", json={"values": {"name": "hijacked"}})
    assert denied.status_code == 403
    after = admin.get("/api/v1/models/core.audit_log", params={"domain": audit_domain, "fields": "id"}).json()["total"]
    assert after == before
    assert (
        admin.get(f"/api/v1/models/core.user/{other['id']}", params={"fields": "name"}).json()["name"] == other["login"]
    )


# -- view schema errors (1.2-f) ------------------------------------------------------------------------------


def test_view_schema_errors_name_file_and_pointer(env, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from modules.web.loaders import load_views
    from tessera.modules.manifest import Manifest

    bad = [
        {
            "id": "view_bad",
            "model": "ktest.book",
            "type": "form",
            "root": {"kind": "sheet", "children": [{"kind": "field", "name": "name"}, {"kind": "gizmo"}]},
        }
    ]
    path = tmp_path / "broken_views.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(ModuleError) as excinfo:
        load_views(env, Manifest(name="kernel_test_a", path=tmp_path), path, "install")
    message = str(excinfo.value)
    assert "broken_views.json[0]" in message and "gizmo" in message and "/root/children/1" in message
    with pytest.raises(ValidationError, match="component"):
        env["web.view"].create(
            {"name": "c", "model_name": "ktest.book", "type": "form", "definition": {"root": {"kind": "component"}}}
        )


# -- webhook retries (1.2-e) ---------------------------------------------------------------------------------


@respx.mock
def test_webhook_retry_backoff(env) -> None:  # type: ignore[no-untyped-def]
    route = respx.post("https://hooks.example.test/flaky").mock(
        side_effect=[Response(500), Response(503), Response(200)]
    )
    hook = env["core.webhook"].create(
        {"name": "flaky", "model_name": "ktest.tag", "url": "https://hooks.example.test/flaky", "events": ["create"]}
    )
    delivery = (
        env["core.webhook_delivery"]
        .elevated()
        .create({"webhook_id": hook.ids[0], "event": "create", "model": "ktest.tag", "res_id": 1, "payload": {"x": 1}})
    )
    assert delivery.deliver() is False
    assert delivery.status == "failed" and delivery.attempts == 1
    first_delay = delivery.next_attempt_at - env.now()
    assert dt.timedelta(minutes=1) < first_delay <= dt.timedelta(minutes=2, seconds=5)
    deliveries = env["core.webhook_delivery"]
    assert deliveries.retry_due() == 0  # backoff not elapsed
    delivery.write({"next_attempt_at": env.now() - dt.timedelta(seconds=1)})
    assert deliveries.retry_due() == 1
    assert delivery.attempts == 2 and delivery.status == "failed"
    assert dt.timedelta(minutes=3) < delivery.next_attempt_at - env.now() <= dt.timedelta(minutes=4, seconds=5)
    delivery.write({"next_attempt_at": env.now() - dt.timedelta(seconds=1)})
    deliveries.retry_due()
    assert delivery.status == "sent" and delivery.attempts == 3 and route.call_count == 3
