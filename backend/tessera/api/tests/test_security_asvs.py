"""Regression tests of the security reviews (``docs/security/asvs-phase-1.md`` SEC-01…SEC-17,
``docs/security/asvs-phase-2.md`` SEC-50 onwards).

Each test is named after the finding it closes (SEC-xx) and fails on the code before the fix.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pyotp
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from tessera.api.middleware import limiter
from tessera.api.tests.conftest import Session
from tessera.config import Settings, get_settings
from tessera.exceptions import AccessDenied, AccessError, UserError

pytestmark = pytest.mark.db


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _new_user(admin: Session, *group_xmlids: str, password: str = "password1234") -> dict[str, Any]:
    group_ids = []
    for xmlid in group_xmlids:
        module, name = xmlid.split(".")
        rows = admin.get(
            "/api/v1/models/core.module_record",
            params={"domain": json.dumps([["module", "=", module], ["name", "=", name]]), "fields": "res_id"},
        ).json()["items"]
        group_ids.append(rows[0]["res_id"])
    login = _uniq("sec").lower()
    vals = {"login": login, "name": login, "password": password, "group_ids": [["set", group_ids]]}
    created = admin.post("/api/v1/models/core.user", json={"values": vals, "fields": ["login"]})
    assert created.status_code == 201, created.text
    return {"id": created.json()["id"], "login": login, "password": password}


def _draft_book(admin: Session) -> int:
    made = admin.post("/api/v1/models/ktest.book", json={"values": {"name": _uniq("secret draft"), "status": "draft"}})
    assert made.status_code == 201, made.text
    return int(made.json()["id"])


# -- SEC-01: a caller-supplied context cannot widen the company scope -----------------------------------------


def test_sec01_client_context_cannot_widen_company_scope(commit_env, reader: Session) -> None:  # type: ignore[no-untyped-def]
    main = commit_env.ref("core.main_company")
    other = commit_env["core.company"].create({"name": _uniq("Other Co"), "currency_id": main.currency_id.ids[0]})
    book = commit_env["ktest.book"].create({"name": _uniq("other company book"), "status": "published"})
    book.write({"company_id": other.ids[0]})
    commit_env.cr.commit()
    plain = reader.get(f"/api/v1/models/ktest.book/{book.ids[0]}")
    assert plain.status_code in (403, 404)
    widened = reader.get(
        f"/api/v1/models/ktest.book/{book.ids[0]}",
        params={"context": json.dumps({"allowed_company_ids": [other.ids[0]], "company_id": other.ids[0]})},
    )
    assert widened.status_code in (403, 404), widened.text
    listed = reader.get(
        "/api/v1/models/ktest.book",
        params={
            "domain": json.dumps([["id", "=", book.ids[0]]]),
            "context": json.dumps({"allowed_company_ids": [other.ids[0]]}),
        },
    ).json()
    assert listed["items"] == []


def test_sec01_client_context_keeps_presentation_keys_only() -> None:
    from tessera.api.routers.records import client_context

    kept = client_context(
        {
            "lang": "fr_FR",
            "active_test": False,
            "default_status": "draft",
            "allowed_company_ids": [99],
            "company_id": 99,
            "workflow_transition": 1,
            "automation_disable": True,
            "tracking_disable": True,
            "force_delete": True,
            "_recompute_write": True,
        }
    )
    assert kept == {"lang": "fr_FR", "active_test": False, "default_status": "draft"}


# -- SEC-02: owner-scoped records cannot be handed to another user ----------------------------------------------


def test_sec02_api_key_cannot_be_repointed_at_another_user(app, librarian: Session) -> None:  # type: ignore[no-untyped-def]
    created = librarian.post("/api/v1/auth/api-keys", json={"name": "mine"}).json()
    # JSON-RPC ``write`` answers ``true`` without reading the record back, so nothing else stops the write
    moved = librarian.post(
        "/api/v1/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "write",
            "params": {"model": "core.api_key", "ids": [created["id"]], "values": {"user_id": 2}},
        },
    ).json()
    assert "error" in moved and moved["error"]["data"]["error"] == "AccessError", moved
    patched = librarian.patch(f"/api/v1/models/core.api_key/{created['id']}", json={"values": {"user_id": 2}})
    assert patched.status_code == 403
    anon = TestClient(app, base_url="http://localhost")
    info = anon.get("/api/v1/auth/session", headers={"Authorization": f"Bearer {created['key']}"}).json()
    assert info["user"]["login"] == "librarian"


def test_sec02_record_cannot_be_moved_to_a_foreign_company(commit_env, librarian: Session) -> None:  # type: ignore[no-untyped-def]
    main = commit_env.ref("core.main_company")
    other = commit_env["core.company"].create({"name": _uniq("Foreign Co"), "currency_id": main.currency_id.ids[0]})
    commit_env.cr.commit()
    book = librarian.post("/api/v1/models/ktest.book", json={"values": {"name": _uniq("mine")}}).json()
    moved = librarian.post(
        "/api/v1/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "write",
            "params": {"model": "ktest.book", "ids": [book["id"]], "values": {"company_id": other.ids[0]}},
        },
    ).json()
    assert "error" in moved and moved["error"]["data"]["error"] == "AccessError", moved
    assert librarian.get(f"/api/v1/models/ktest.book/{book['id']}").status_code == 200


# -- SEC-03: attachments follow the access of the record they belong to -----------------------------------------


def test_sec03_attachment_of_unreadable_record_is_hidden_from_the_generic_api(admin: Session, reader: Session) -> None:
    book_id = _draft_book(admin)  # readers only see published books (and their own)
    up = admin.post(
        "/api/v1/attachments",
        files={"file": ("contract.txt", b"confidential terms", "text/plain")},
        data={"model": "ktest.book", "res_id": str(book_id)},
    ).json()
    att = up["id"]
    # read through the REST API, the JSON-RPC API and the listing
    assert reader.get(f"/api/v1/models/core.attachment/{att}", params={"fields": "raw"}).status_code == 403
    assert reader.get(f"/api/v1/models/core.attachment/{att}", params={"fields": "name"}).status_code == 403
    rpc = reader.post(
        "/api/v1/jsonrpc",
        json={"jsonrpc": "2.0", "id": 1, "method": "read", "params": {"model": "core.attachment", "ids": [att]}},
    ).json()
    assert "error" in rpc
    listed = reader.get(
        "/api/v1/models/core.attachment", params={"domain": json.dumps([["id", "=", att]]), "fields": "name"}
    ).json()
    assert listed["items"] == []
    # nor can it be changed, published or shared
    assert reader.patch(f"/api/v1/models/core.attachment/{att}", json={"values": {"public": True}}).status_code == 403
    shared = reader.post(f"/api/v1/models/core.attachment/{att}/call/generate_access_token", json={})
    assert shared.status_code == 403
    assert reader.get(f"/api/v1/attachments/{att}/content").status_code == 403
    # the owner of the record still can
    assert admin.get(f"/api/v1/attachments/{att}/content").content == b"confidential terms"


def test_sec03_unlinked_upload_belongs_to_its_uploader(admin: Session, reader: Session, librarian: Session) -> None:
    up = reader.post("/api/v1/attachments", files={"file": ("draft.txt", b"my notes", "text/plain")}).json()
    assert reader.get(f"/api/v1/attachments/{up['id']}/content").content == b"my notes"
    assert librarian.get(f"/api/v1/attachments/{up['id']}/content").status_code == 403
    assert librarian.get(f"/api/v1/models/core.attachment/{up['id']}", params={"fields": "raw"}).status_code == 403


def test_sec03_context_default_cannot_set_restricted_field(admin: Session, reader: Session) -> None:
    secret = admin.post(
        "/api/v1/attachments", files={"file": ("payroll.txt", b"salaries", "text/plain")}, data={"public": "false"}
    ).json()
    store_key = f"{secret['checksum'][:2]}/{secret['checksum'][2:4]}/{secret['checksum']}"
    rpc = reader.post(
        "/api/v1/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "create",
            "params": {
                "model": "core.attachment",
                "values": {"name": "x"},
                "context": {"default_store_key": store_key},
            },
        },
    ).json()
    assert "error" in rpc and rpc["error"]["data"]["error"] == "AccessError", rpc


# -- SEC-04: downloads cannot run script in the application's origin --------------------------------------------


def test_sec04_active_content_is_downloaded_not_rendered(admin: Session) -> None:
    book_id = _draft_book(admin)
    up = admin.post(
        "/api/v1/attachments",
        files={"file": ('évil".html', b"<script>alert(1)</script>", "text/html")},
        data={"model": "ktest.book", "res_id": str(book_id)},
    ).json()
    down = admin.get(f"/api/v1/attachments/{up['id']}/content")
    assert down.status_code == 200
    disposition = down.headers["content-disposition"]
    assert disposition.startswith("attachment;") and "filename*=UTF-8''%C3%A9vil" in disposition
    assert "default-src 'none'" in down.headers["content-security-policy"]
    assert down.headers["x-content-type-options"] == "nosniff"
    text = admin.post(
        "/api/v1/attachments",
        files={"file": ("ok.txt", b"plain", "text/plain")},
        data={"model": "ktest.book", "res_id": str(book_id)},
    ).json()
    assert admin.get(f"/api/v1/attachments/{text['id']}/content").headers["content-disposition"].startswith("inline;")


# -- SEC-05: a Studio designer cannot grant themselves rights through an automation ------------------------------


def test_sec05_designer_automation_cannot_target_platform_models(env, env_for) -> None:  # type: ignore[no-untyped-def]
    from modules.studio import service as S

    designer_group = env.ref("studio.group_designer")
    login = _uniq("designer").lower()
    env["core.user"].create({"login": login, "name": login, "group_ids": [("set", [designer_group.ids[0]])]})
    designer = env_for(login)
    grant_key = {
        "name": "backdoor",
        "model_name": "ktest.book",
        "trigger": "on_create",
        "actions": [
            {
                "type": "create_record",
                "model": "core.api_key",
                "values": {"name": "'k'", "user_id": "2", "key_hash": "'" + "0" * 64 + "'"},
            }
        ],
    }
    with pytest.raises(AccessError):
        S.add_automation(designer, grant_key)
    on_users = {
        "name": "promote",
        "model_name": "core.user",
        "trigger": "on_write",
        "actions": [{"type": "update", "values": {"active": "True"}}],
    }
    with pytest.raises(AccessError):
        S.add_automation(designer, on_users)
    S._check_automation_scope(env, grant_key)  # administrators keep full control


# -- SEC-06: printing never reads local files or URLs, nor secrets ---------------------------------------------


def test_sec06_pdf_rendering_loads_data_uris_only() -> None:
    from tessera.reports.engine import render_pdf

    pdf = render_pdf(
        '<p>x</p><a rel="attachment" href="file:///etc/hostname">a</a>'
        '<img src="file:///etc/hostname"><img src="http://169.254.169.254/latest/meta-data/">'
    )
    assert pdf.startswith(b"%PDF") and b"/EmbeddedFile" not in pdf


def test_sec06_templates_cannot_print_administrator_only_fields(env) -> None:  # type: ignore[no-untyped-def]
    from tessera.reports.engine import render_html, wrap

    admin_user = env["core.user"].elevated().browse([2])
    with pytest.raises(UserError):
        render_html("{{ user.password_hash }}", {"docs": None, "user": wrap(admin_user)})
    assert admin_user.login in render_html("{{ user.login }}", {"docs": None, "user": wrap(admin_user)})


# -- SEC-07: OIDC links an existing account only through a verified e-mail ----------------------------------------


def test_sec07_oidc_unverified_email_does_not_take_over_an_account(env) -> None:  # type: ignore[no-untyped-def]
    from tessera.security.auth import user_for_oidc_claims

    sys_env = env.elevated()
    provider = sys_env["core.sso_provider"].create(
        {"name": "IdP", "code": _uniq("idp"), "issuer": "https://idp.example.test", "client_id": "c"}
    )
    admin_login = sys_env["core.user"].browse([2]).login
    claims = {"sub": _uniq("attacker"), "email": admin_login}
    with pytest.raises(AccessDenied):
        user_for_oidc_claims(sys_env, provider, claims)
    with pytest.raises(AccessDenied):
        user_for_oidc_claims(sys_env, provider, {**claims, "email_verified": False})
    linked = user_for_oidc_claims(sys_env, provider, {**claims, "email_verified": True})
    assert linked.ids == [2]


# -- SEC-08: the second factor resists brute force and silent removal -------------------------------------------


def test_sec08_totp_failures_lock_the_account_and_setup_cannot_replace_it(app, admin: Session) -> None:  # type: ignore[no-untyped-def]
    user = _new_user(admin, "kernel_test_a.group_reader")
    me = Session(TestClient(app, base_url="http://localhost"), user["login"], user["password"])
    secret = me.post("/api/v1/auth/totp/setup").json()["secret"]
    assert me.post("/api/v1/auth/totp/enable", json={"code": pyotp.TOTP(secret).now()}).status_code == 200
    # a second setup would drop the enabled factor without a code
    assert me.post("/api/v1/auth/totp/setup").status_code == 400
    client = TestClient(app, base_url="http://localhost")
    for _ in range(get_settings().login_lockout_attempts):
        limiter._hits.clear()
        step = client.post("/api/v1/auth/login", json={"login": user["login"], "password": user["password"]}).json()
        bad = client.post("/api/v1/auth/totp", json={"pre_auth_token": step["pre_auth_token"], "code": "000000"})
        assert bad.status_code == 401
    limiter._hits.clear()
    locked = client.post("/api/v1/auth/login", json={"login": user["login"], "password": user["password"]})
    assert locked.status_code == 401 and locked.json().get("locked") is True


# -- SEC-09: X-Forwarded-For is only believed from a trusted proxy -----------------------------------------------


def _request(peer: str, forwarded: str | None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "client": (peer, 1234)})


def test_sec09_forwarded_for_is_only_trusted_from_proxies() -> None:
    from tessera.api.middleware import client_ip

    assert client_ip(_request("203.0.113.5", "1.2.3.4")) == "203.0.113.5"  # direct client: header ignored
    # behind the proxy: the address the proxy appended, not what the client wrote before it
    assert client_ip(_request("10.0.0.2", "1.2.3.4, 198.51.100.7")) == "198.51.100.7"
    assert client_ip(_request("127.0.0.1", "198.51.100.7, 10.0.0.9")) == "198.51.100.7"
    assert client_ip(_request("10.0.0.2", None)) == "10.0.0.2"


def test_sec09_spoofed_forwarded_for_does_not_reset_the_login_limit(client: TestClient) -> None:
    limit = get_settings().login_rate_limit_per_minute
    codes = [
        client.post(
            "/api/v1/auth/login",
            json={"login": "nobody", "password": "x"},
            headers={"X-Forwarded-For": f"198.51.100.{i}"},
        ).status_code
        for i in range(limit + 1)
    ]
    assert codes[-1] == 429


# -- SEC-10: production refuses development secrets and a wildcard CORS origin ---------------------------------


def test_sec10_production_check_refuses_dev_master_key_and_wildcard_cors() -> None:
    from tessera.config import DEV_MASTER_KEY

    base = {"env": "production", "secret_key": "x" * 40, "ai_gateway_token": "t" * 32}
    with pytest.raises(RuntimeError, match="TESSERA_MASTER_KEY"):
        Settings(**base, master_key=DEV_MASTER_KEY).check()
    good_key = "cmFuZG9tLXByb2R1Y3Rpb24ta2V5LXdpdGgtMzItYnl0ZXMhIQ=="
    with pytest.raises(RuntimeError, match="TESSERA_SECRET_KEY"):
        Settings(
            **{**base, "secret_key": "change-me-32-bytes-minimum-for-session-signing"}, master_key=good_key
        ).check()
    with pytest.raises(RuntimeError, match="CORS"):
        Settings(**base, master_key=good_key, cors_origins=["*"]).check()
    Settings(**base, master_key=good_key).check()


# -- SEC-11: user-configured outbound calls cannot reach the server's own network ---------------------------------


def test_sec11_outbound_url_policy() -> None:
    from tessera.net import OutboundRefused, check_outbound_url

    def resolver(answer: str):  # type: ignore[no-untyped-def]
        return lambda host, port: [answer]

    for url in ("http://169.254.169.254/latest/meta-data/", "http://127.0.0.1:8100/v1", "http://[::1]/", "file:///etc"):
        with pytest.raises(OutboundRefused):
            check_outbound_url(url, allow_private=False)
    with pytest.raises(OutboundRefused):
        check_outbound_url("https://internal.example.test/", resolver=resolver("10.1.2.3"), allow_private=False)
    assert check_outbound_url("https://hooks.example.test/x", resolver=resolver("93.184.215.14"), allow_private=False)
    assert check_outbound_url("http://localhost:9000/", allow_private=True)


def test_sec11_caldav_refuses_private_addresses_in_production(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import httpx

    from modules.calendar.sync.caldav import CalDavAdapter
    from tessera.net import OutboundRefused

    monkeypatch.setattr(get_settings(), "outbound_allow_private", False)
    with pytest.raises(OutboundRefused):
        CalDavAdapter("http://169.254.169.254/dav/", None, None)
    # a public server cannot send the client on to an internal address either
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    adapter = CalDavAdapter("https://93.184.215.14/dav/", None, None, client=client)
    with pytest.raises(OutboundRefused):
        adapter.client.get("http://10.0.0.5/secret.ics")


# -- SEC-12: API-rendered HTML runs no script; request ids cannot forge log lines ----------------------------------


def test_sec12_api_html_gets_a_content_security_policy() -> None:
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse

    from tessera.api.middleware import SecurityHeadersMiddleware

    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/v1/page")
    def page() -> HTMLResponse:
        return HTMLResponse("<p>hi</p>")

    @app.get("/api/v1/data")
    def data() -> dict[str, str]:
        return {"ok": "yes"}

    with TestClient(app) as c:
        assert "default-src 'none'" in c.get("/api/v1/page").headers["content-security-policy"]
        assert "content-security-policy" not in c.get("/api/v1/data").headers


def test_sec12_request_id_header_is_sanitised(client: TestClient) -> None:
    forged = 'abc" level=error msg="forged'
    assert client.get("/healthz", headers={"X-Request-Id": forged}).headers["x-request-id"] != forged
    assert client.get("/healthz", headers={"X-Request-Id": "req-123"}).headers["x-request-id"] == "req-123"


# -- SEC-13: the merge undo log cannot inject SQL identifiers ----------------------------------------------------


def test_sec13_merge_undo_log_identifiers_are_checked(env) -> None:  # type: ignore[no-untyped-def]
    from modules.contacts.merge import _check_identifiers

    with pytest.raises(UserError):
        _check_identifiers(env, {"generic": [{"table": "core_user; DROP TABLE x", "column": "id", "rows": []}]})
    with pytest.raises(UserError):
        _check_identifiers(env, {"generic": [{"table": "no_such_table", "column": "id", "rows": []}]})
    _check_identifiers(env, {"generic": [{"table": "core_attachment", "column": "res_id", "rows": []}]})


# -- SEC-14: one JSON-RPC request cannot carry an unbounded batch ------------------------------------------------


def test_sec14_jsonrpc_batch_is_capped(admin: Session) -> None:
    from tessera.api.routers.jsonrpc import MAX_BATCH

    call = {"jsonrpc": "2.0", "method": "search_count", "params": {"model": "ktest.tag"}}
    ok = admin.post("/api/v1/jsonrpc", json=[{**call, "id": i} for i in range(3)]).json()
    assert isinstance(ok, list) and len(ok) == 3
    refused = admin.post("/api/v1/jsonrpc", json=[{**call, "id": i} for i in range(MAX_BATCH + 1)]).json()
    assert isinstance(refused, dict) and refused["error"]["code"] == -32600


# ===============================================================================================================
# Phase 2 review (docs/security/asvs-phase-2.md)
# ===============================================================================================================


def _p2_user(env: Any, login: str, groups: list[str], company: Any | None = None) -> Any:
    """An environment of a new user of ``company`` (default: the current one) with exactly ``groups``."""
    company = company or env.company
    user = env["core.user"].create(
        {
            "name": login,
            "login": _uniq(login),
            "company_id": company.ids[0],
            "company_ids": [("set", company.ids)],
            "group_ids": [("set", [env.ref(g).ids[0] for g in groups])],
        }
    )
    return env.with_user(user.ids[0])


# -- Every Phase 2 model: company isolation (BUILD_PLAN §11 "authz tests for every new model") -------------------

PHASE2_MODULES = {
    "account",
    "account_analytic",
    "account_asset",
    "account_bank",
    "account_budget",
    "account_dunning",
    "account_einvoice",
    "account_reports",
    "account_tax",
    "auth_saml",
    "payment",
    "product",
    "sale",
    "sale_commission",
    "sale_rental",
    "sale_subscription",
}
# models without a stored company: shared definitions, or children only reached through a company-scoped parent
SHARED_PHASE2_MODELS = {
    "account.account_tag",  # tax grids, shared by the charts
    "account.fiscal_position_account",  # SEC-60: lines of a company-scoped fiscal position
    "account.fiscal_position_tax",  # SEC-60
    "account.full_reconcile",  # SEC-60: matching numbers; the matched items are company-scoped
    "account.payment_term_line",  # SEC-60: lines of a company-scoped payment term
    "account.report",  # report definitions (data), shared by every company
    "account.report_line",
    "auth_saml.assertion",  # administrators only
    "auth_saml.identity",
    "auth_saml.request",
    "product.attribute",  # catalogue definitions shared like Phase 1 categories and units
    "product.attribute_line",
    "product.attribute_value",
    "product.category",
    "product.uom",
    "sale.subscription_close_reason",
}


def test_sec_phase2_every_company_model_is_isolated(env) -> None:  # type: ignore[no-untyped-def]
    """Every Phase 2 model has a stored ``company_id`` (so the multi-company rule applies) or is a listed shared
    model; a user of another company with the widest business roles sees none of the EU company's records."""
    from modules.account.tests.common import eu_env, invoice, partner
    from tessera.orm import fields as orm_fields

    e = eu_env(env)
    eu = e.company
    customer = partner(e, "Isolation SA", company_id=eu.ids[0])
    invoice(e, customer, [{"price_unit": 10.0}]).action_register_payment()  # entries, items, payment, matches
    e["product.product"].create({"name": "Isolation product", "company_id": eu.ids[0]})
    e["sale.quotation"].create({"partner_id": customer.ids[0], "company_id": eu.ids[0]})
    groups = ["account.group_account_manager", "sale.group_sale_manager", "sale.group_sale_all"]
    outsider = _p2_user(env, "isolation", [*groups, "product.group_product_manager"], env.ref("core.main_company"))
    unscoped, checked = [], []
    for name, cls in sorted(env.registry.models.items()):
        if getattr(cls, "_module", None) not in PHASE2_MODULES or getattr(cls, "_abstract", False):
            continue
        field = cls._fields.get("company_id")
        if not (field is not None and field.store and isinstance(field, orm_fields.Many2one)):
            if name not in SHARED_PHASE2_MODELS:
                unscoped.append(name)
            continue
        theirs = env[name].elevated().with_context(active_test=False).search([("company_id", "=", eu.ids[0])])
        if not theirs:
            continue
        try:
            seen = outsider[name].with_context(active_test=False).search([("id", "in", theirs.ids)])
        except AccessError:
            continue  # no read right at all
        assert not seen, f"{name}: {len(seen)} record(s) of another company visible"
        checked.append(name)
    assert not unscoped, f"company-specific models without a stored company_id: {unscoped}"
    expected = {"account.move", "account.move_line", "account.payment", "account.partial_reconcile", "sale.quotation"}
    assert expected <= set(checked), checked


# -- SEC-50: the ledger's own fields cannot be written through the generic API ----------------------------------


def test_sec50_ledger_fields_cannot_be_set_through_the_api(commit_env, admin: Session, app) -> None:  # type: ignore[no-untyped-def]
    # a company of its own, so the committed invoice does not change the main company's books
    main = commit_env.ref("core.main_company")
    company = commit_env["core.company"].create({"name": _uniq("Ledger Co"), "currency_id": main.currency_id.ids[0]})
    clerk = _new_user(admin, "account.group_account_invoice")
    commit_env["core.user"].browse([clerk["id"]]).write(
        {"company_id": company.ids[0], "company_ids": [("set", company.ids)]}
    )
    commit_env.cr.commit()
    s = Session(TestClient(app, base_url="http://localhost"), clerk["login"], clerk["password"])
    customer = s.post("/api/v1/models/contacts.partner", json={"values": {"name": _uniq("Ledger customer")}}).json()
    base = {
        "move_type": "out_invoice",
        "partner_id": customer["id"],
        "invoice_date": "2026-05-04",
        "invoice_line_ids": [["create", {"name": "Service", "price_unit": 100.0}]],
    }
    # a posted entry with a number of the caller's choice, skipping numbering, lock dates and the hash chain
    forged = s.post("/api/v1/models/account.move", json={"values": {**base, "state": "posted", "name": "X/1"}})
    assert forged.status_code == 400, forged.text
    assert s.post("/api/v1/models/account.move", json={"values": {**base, "name": "INV/2026/99999"}}).status_code == 400
    # the same through creation defaults in the client context
    by_default = s.post(
        "/api/v1/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "create",
            "params": {
                "model": "account.move",
                "values": base,
                "context": {"default_state": "posted", "default_name": "X/2"},
            },
        },
    ).json()
    assert "error" in by_default, by_default
    made = s.post("/api/v1/models/account.move", json={"values": base})
    assert made.status_code == 201, made.text
    move_id = made.json()["id"]
    assert s.post(f"/api/v1/models/account.move/{move_id}/call/action_post", json={}).status_code == 200
    due = s.get(f"/api/v1/models/account.move/{move_id}", params={"fields": "amount_residual"}).json()
    # marking it paid without a payment
    paid = s.patch(f"/api/v1/models/account.move/{move_id}", json={"values": {"payment_state": "paid"}})
    assert paid.status_code == 400, paid.text
    rpc = s.post(
        "/api/v1/jsonrpc",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "write",
            "params": {"model": "account.move", "ids": [move_id], "values": {"amount_residual": 0}},
        },
    ).json()
    assert "error" in rpc, rpc
    items = s.get(
        "/api/v1/models/account.move_line",
        params={"domain": json.dumps([["move_id", "=", move_id]]), "fields": "display_type"},
    ).json()["items"]
    term = next(i["id"] for i in items if i["display_type"] == "payment_term")
    income = next(i["id"] for i in items if i["display_type"] == "product")
    for values in ({"reconciled": True, "amount_residual": 0}, {"parent_state": "draft"}):
        changed = s.patch(f"/api/v1/models/account.move_line/{term}", json={"values": values})
        assert changed.status_code == 400, changed.text
    match = s.post(
        "/api/v1/models/account.partial_reconcile",
        json={"values": {"debit_move_id": term, "credit_move_id": income, "amount": due["amount_residual"]}},
    )
    assert match.status_code == 400, match.text
    after = s.get(f"/api/v1/models/account.move/{move_id}", params={"fields": "payment_state,amount_residual"}).json()
    assert after["payment_state"] == "not_paid" and after["amount_residual"] == due["amount_residual"] > 0


# -- SEC-51: a payment cannot cancel a journal entry it did not book ---------------------------------------------


def test_sec51_payment_cannot_cancel_another_entry(env) -> None:  # type: ignore[no-untyped-def]
    from modules.account.tests.common import eu_env, invoice, partner

    e = eu_env(env)
    foreign = invoice(e, partner(e, "SEC-51 Lyon SA"), [{"price_unit": 10.0}])
    clerk = _p2_user(env, "sec51-clerk", ["account.group_account_invoice"], env.ref("core.main_company"))
    journal = clerk["account.journal"].search([("type", "=", "bank")], limit=1)
    customer = partner(clerk, "SEC-51 customer")
    vals = {"amount": 5.0, "partner_id": customer.ids[0], "journal_id": journal.ids[0]}
    with pytest.raises(UserError):
        clerk["account.payment"].create({**vals, "move_id": foreign.ids[0]})
    with pytest.raises(UserError):
        clerk["account.payment"].create({**vals, "state": "posted"})
    payment = clerk["account.payment"].create(vals)
    with pytest.raises(UserError):
        payment.write({"move_id": foreign.ids[0]})
    # even with the link forced in the database, cancelling only touches the payment's own entry
    payment.elevated().write({"move_id": foreign.ids[0]})
    with pytest.raises(UserError):
        payment.action_cancel()
    assert foreign.state == "posted"


# -- SEC-52: privileged actions are not open to every user who can read the record --------------------------------


def test_sec52_privileged_actions_need_the_right_role(env) -> None:  # type: ignore[no-untyped-def]
    import datetime as dt

    from modules.account.tests.common import invoice, partner

    plain = _p2_user(env, "sec52-plain", ["core.group_user"])
    accountant = _p2_user(env, "sec52-accountant", ["account.group_account_user"])
    auditor = _p2_user(env, "sec52-auditor", ["account.group_account_readonly"])
    year = env["account.fiscal_year"].create(
        {"name": _uniq("FY"), "date_from": dt.date(2093, 1, 1), "date_to": dt.date(2093, 12, 31)}
    )
    # closing a year sets the company's lock date: advisers only (every user can read fiscal years)
    for user_env in (plain, accountant):
        with pytest.raises(AccessError):
            user_env["account.fiscal_year"].browse(year.ids).action_close()
    assert year.state == "open"
    # charging a customer's saved card
    provider = env.ref(f"payment.{env.company.ids[0]}_provider_test")
    provider.write({"state": "test"})
    customer = partner(env, "SEC-52 customer")
    token = env["payment.token"].create(
        {"provider_id": provider.ids[0], "partner_id": customer.ids[0], "provider_ref": "c:pm", "display": "Visa"}
    )
    with pytest.raises(AccessError):
        auditor["payment.token"].browse(token.ids).action_charge(500.0)
    assert not env["payment.transaction"].search([("token_id", "=", token.ids[0])])
    # e-mailing an invoice (stores a PDF, posts in the chatter, sends the access link anywhere)
    move = invoice(env, customer, [{"price_unit": 10.0}])
    with pytest.raises(AccessError):
        auditor["account.move"].browse(move.ids).action_send("someone@example.test")
    with pytest.raises(AccessError):
        auditor["account.move"].browse(move.ids).action_generate_ubl()
    # matching bank transactions (runs elevated, posts entries)
    with pytest.raises(AccessError):
        auditor["account.bank_statement_line"].action_auto_reconcile_all()


# -- SEC-53: payment transactions and saved methods follow the provider -----------------------------------------


def test_sec53_payment_records_are_provider_managed(env) -> None:  # type: ignore[no-untyped-def]
    from modules.account.tests.common import invoice, partner
    from modules.payment import service

    provider = env.ref(f"payment.{env.company.ids[0]}_provider_test")
    provider.write({"state": "test"})
    customer = partner(env, "SEC-53 customer")
    move = invoice(env, customer, [{"price_unit": 40.0}])
    tx = service.create_invoice_transaction(move)
    token = env["payment.token"].create(
        {"provider_id": provider.ids[0], "partner_id": customer.ids[0], "provider_ref": "c:pm", "display": "Visa"}
    )
    clerk = _p2_user(env, "sec53-clerk", ["account.group_account_invoice"])
    for values in ({"amount": 1.0}, {"state": "done"}, {"provider_reference": "cs_someone_else"}):
        with pytest.raises(UserError):
            clerk["payment.transaction"].browse(tx.ids).write(values)
    with pytest.raises(UserError):
        clerk["payment.token"].browse(token.ids).write({"partner_id": partner(env, "SEC-53 other").ids[0]})
    clerk["payment.token"].browse(token.ids).write({"display": "Company card", "active": False})
    assert float(tx.amount) == float(move.amount_residual) and tx.state == "draft"
    assert token.partner_id.ids == customer.ids


# -- SEC-54: the test payment provider does not exist in production ---------------------------------------------


def test_sec54_test_payment_provider_is_refused_in_production(env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from modules.payment.providers import ProviderError, adapter_for

    provider = env.ref(f"payment.{env.company.ids[0]}_provider_test")
    provider.write({"state": "test"})
    assert adapter_for(provider) is not None
    assert provider.ids[0] in env["payment.provider"]._available(env.company).ids
    monkeypatch.setattr(get_settings(), "env", "production")
    with pytest.raises(ProviderError):
        adapter_for(provider)
    assert provider.ids[0] not in env["payment.provider"]._available(env.company).ids


# -- SEC-55: report exports never hand a formula to a spreadsheet ----------------------------------------------


def test_sec55_report_exports_neutralise_formulas() -> None:
    import io

    from openpyxl import load_workbook

    from modules.account_reports.export import _csv, _xlsx

    data = {
        "report": {"name": "General ledger", "code": "gl"},
        "columns": [{"label": "Label", "type": "text"}, {"label": "Balance", "type": "monetary"}],
        "lines": [{"name": '=HYPERLINK("http://evil.test","x")', "level": 0, "values": ["@SUM(1+1)", -12.5]}],
        "currency": {"decimal_places": 2, "name": "EUR"},
        "companies": [{"name": "Acme"}],
        "warnings": [],
    }
    text = _csv(data).decode()
    assert "'=HYPERLINK" in text and "'@SUM" in text and "-12.5" in text
    sheet = load_workbook(io.BytesIO(_xlsx(data))).active
    cells = [cell for row in sheet.iter_rows() for cell in row if cell.value is not None]
    assert not [cell.coordinate for cell in cells if cell.data_type == "f"]
    assert -12.5 in [cell.value for cell in cells]


# -- SEC-56: names in outgoing e-mails are text -------------------------------------------------------------------


def test_sec56_invoice_email_escapes_names(env) -> None:  # type: ignore[no-untyped-def]
    from modules.account.tests.common import invoice, partner

    customer = partner(env, "<a href='https://evil.test'>Acme</a>")
    move = invoice(env, customer, [{"price_unit": 10.0}])
    move.action_send("ap@acme.test")
    mail = env["core.mail_queue"].elevated().search([("model", "=", "account.move"), ("res_id", "=", move.ids[0])])
    assert mail and "<a href='https://evil.test'>" not in mail.body_html and "&lt;a href=" in mail.body_html
