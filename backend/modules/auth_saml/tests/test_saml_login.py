"""SAML sign-in end to end: endpoints, pending requests bound to the browser, replay cache, account mapping,
administration and access rights."""

from __future__ import annotations

import base64
import datetime as dt
import urllib.parse
import uuid
import zlib
from typing import Any

import pyotp
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi.testclient import TestClient
from lxml import etree

from modules.auth_saml import saml, service
from modules.auth_saml.tests.idp import SSO_URL, FakeIdp, encode, new_identity
from tessera.api.middleware import limiter
from tessera.api.tests.conftest import Session
from tessera.config import get_settings
from tessera.exceptions import AccessDenied, AccessError, ValidationError

pytestmark = pytest.mark.db


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def idp() -> FakeIdp:
    return FakeIdp()


def _provider_vals(idp: FakeIdp, **vals: Any) -> dict[str, Any]:
    return {
        "name": _uniq("Corporate SSO"),
        "idp_entity_id": idp.entity_id,
        "idp_sso_url": SSO_URL,
        "idp_certificates": idp.cert_pem,
        **vals,
    }


@pytest.fixture
def make_provider(commit_env, idp: FakeIdp):  # type: ignore[no-untyped-def]
    """Committed providers (the API runs in its own transactions)."""

    def factory(**vals: Any) -> Any:
        provider = commit_env["auth_saml.provider"].create(_provider_vals(idp, **vals))
        commit_env.cr.commit()
        return provider

    return factory


def _committed_user(commit_env, **vals: Any) -> Any:  # type: ignore[no-untyped-def]
    login = vals.pop("login", None) or f"{_uniq('saml')}@example.test"
    internal = commit_env.ref("core.group_user")
    user = commit_env["core.user"].create(
        {"login": login, "name": login, "email": login, "group_ids": [("set", internal.ids)], **vals}
    )
    commit_env.cr.commit()
    return user


class Browser:
    """A browser signing in through the identity provider (service-provider initiated)."""

    def __init__(self, app: Any, idp: FakeIdp, provider: Any) -> None:  # type: ignore[no-untyped-def]
        self.client = TestClient(app, base_url="http://localhost")
        self.idp = idp
        self.pid = provider.ids[0]
        self.acs = provider.acs()
        self.audience = provider.entity_id()

    def start(self) -> tuple[str, str, str]:
        resp = self.client.get(f"/api/v1/auth/saml/{self.pid}/login", follow_redirects=False)
        assert resp.status_code == 302, resp.text
        location = resp.headers["location"]
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(location).query))
        request = etree.fromstring(zlib.decompress(base64.b64decode(query["SAMLRequest"]), -15))
        return request.get("ID"), query["RelayState"], location

    def response(self, request_id: str | None, *, name_id: str, **kw: Any) -> Any:
        assertion_kw = {k: kw.pop(k) for k in list(kw) if k in ("attributes", "assertion_id", "name_id_format")}
        assertion = self.idp.assertion(
            request_id=request_id, acs_url=self.acs, audience=self.audience, name_id=name_id, **assertion_kw
        )
        return self.idp.response(assertion, acs_url=self.acs, request_id=request_id, **kw)

    def post(self, response: Any, relay_state: str, client: TestClient | None = None) -> Any:
        return (client or self.client).post(
            f"/api/v1/auth/saml/{self.pid}/acs",
            data={"SAMLResponse": encode(response), "RelayState": relay_state},
            follow_redirects=False,
        )

    def login(self, name_id: str, **kw: Any) -> Any:
        request_id, relay, _ = self.start()
        return self.post(self.response(request_id, name_id=name_id, **kw), relay)


# -- endpoints ---------------------------------------------------------------------------------------------


def test_login_creates_a_session_like_a_password_login(app, idp: FakeIdp, make_provider, commit_env) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider(allow_signup=True, name_attribute="displayName")
    browser = Browser(app, idp, provider)
    email = f"{_uniq('jit')}@example.test"
    request_id, relay, location = browser.start()
    assert location.startswith(SSO_URL + "?SAMLRequest=")
    assert "tessera_saml" in browser.client.cookies
    done = browser.post(browser.response(request_id, name_id=email, attributes={"displayName": "Jane Doe"}), relay)
    assert done.status_code == 303, done.text
    assert done.headers["location"] == "/"
    cookies = done.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(get_settings().session_cookie_name + "="))
    assert "HttpOnly" in session_cookie and "samesite=lax" in session_cookie.lower()
    assert any(c.startswith('tessera_saml=""') or "tessera_saml=;" in c for c in cookies)  # binding cookie dropped
    info = browser.client.get("/api/v1/auth/session").json()
    assert info["user"]["login"] == email and info["user"]["name"] == "Jane Doe"
    assert info["auth"] == "session" and info["csrf_token"] and info["user"]["is_admin"] is False
    # the CSRF token works for unsafe requests, like after a password login
    out = browser.client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": info["csrf_token"]})
    assert out.status_code == 200
    commit_env.invalidate_all()
    user = commit_env["core.user"].search([("login", "=", email)])
    assert user.group_ids.ids == commit_env.ref("core.group_user").ids  # internal-user default group only
    link = commit_env["auth_saml.identity"].search([("provider_id", "=", provider.ids[0])])
    assert link.subject == email and link.user_id == user and link.last_login
    # the next login uses the link
    again = Browser(app, idp, provider).login(email)
    assert again.status_code == 303
    assert commit_env["core.user"].search_count([("login", "=", email)]) == 1


def test_public_provider_list_and_inactive_provider(app, idp: FakeIdp, make_provider, admin: Session) -> None:  # type: ignore[no-untyped-def]
    active = make_provider()
    inactive = make_provider(active=False)
    anon = TestClient(app, base_url="http://localhost")
    listed = anon.get("/api/v1/auth/saml/providers").json()
    assert {"id": active.ids[0], "name": active.name} in listed
    assert inactive.ids[0] not in [p["id"] for p in listed]
    assert set(listed[0]) == {"id", "name"}  # nothing of the configuration
    assert anon.get(f"/api/v1/auth/saml/{inactive.ids[0]}/login", follow_redirects=False).status_code == 404
    posted = anon.post(f"/api/v1/auth/saml/{inactive.ids[0]}/acs", data={"SAMLResponse": "x", "RelayState": "y"})
    assert posted.status_code == 404
    assert anon.get(f"/api/v1/auth/saml/{inactive.ids[0]}/metadata").status_code == 404
    assert admin.get(f"/api/v1/auth/saml/{inactive.ids[0]}/metadata").status_code == 200
    assert anon.get("/api/v1/auth/saml/999999/login", follow_redirects=False).status_code == 404


def test_metadata_endpoint(app, make_provider) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider(name_id_format="persistent")
    anon = TestClient(app, base_url="http://localhost")
    resp = anon.get(f"/api/v1/auth/saml/{provider.ids[0]}/metadata")
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("application/xml")
    assert "default-src 'none'" in resp.headers["content-security-policy"]
    doc = etree.fromstring(resp.content)
    md = {"md": saml.NS_MD}
    base = f"{get_settings().public_url.rstrip('/')}/api/v1/auth/saml/{provider.ids[0]}"
    assert doc.get("entityID") == f"{base}/metadata" == provider.sp_entity_id
    assert doc.find(".//md:AssertionConsumerService", md).get("Location") == f"{base}/acs" == provider.acs_url
    assert doc.find(".//md:NameIDFormat", md).text == saml.NAMEID_FORMATS["persistent"]
    assert doc.find(".//md:KeyDescriptor", md) is None
    assert provider.metadata_url == f"{base}/metadata"


def test_signed_login_requests(app, make_provider) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider()
    provider.action_generate_sp_keypair()
    provider.env.cr.commit()
    raw = provider.elevated().read(["sp_private_key"])[0]["sp_private_key"]
    assert raw.startswith("enc:v1:") and provider.sign_requests
    anon = TestClient(app, base_url="http://localhost")
    location = anon.get(f"/api/v1/auth/saml/{provider.ids[0]}/login", follow_redirects=False).headers["location"]
    query = urllib.parse.urlsplit(location).query
    signed, _, signature = query.rpartition("&Signature=")
    cert = saml.load_certificates(provider.sp_certificate)[0]
    cert.public_key().verify(  # type: ignore[union-attr,call-arg]
        base64.b64decode(urllib.parse.unquote(signature)), signed.encode(), padding.PKCS1v15(), hashes.SHA256()
    )
    metadata = etree.fromstring(anon.get(f"/api/v1/auth/saml/{provider.ids[0]}/metadata").content)
    assert metadata.find(f".//{{{saml.NS_MD}}}SPSSODescriptor").get("AuthnRequestsSigned") == "true"
    assert metadata.find(f".//{{{saml.NS_DS}}}X509Certificate").text == saml.certificate_body(cert)


def test_errors_are_refused_without_a_session(app, idp: FakeIdp, make_provider, commit_env) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider()
    user = _committed_user(commit_env)
    commit_env["auth_saml.identity"].create(
        {"provider_id": provider.ids[0], "subject": user.login, "user_id": user.ids[0]}
    )
    commit_env.cr.commit()
    browser = Browser(app, idp, provider)
    request_id, relay, _ = browser.start()
    # wrong audience: the IdP issued the assertion for another application
    bad = browser.idp.response(
        idp.assertion(request_id=request_id, acs_url=browser.acs, audience="https://other/sp", name_id=user.login),
        acs_url=browser.acs,
        request_id=request_id,
    )
    refused = browser.post(bad, relay)
    assert refused.status_code == 401 and refused.json()["message"] == "The assertion is meant for another application"
    assert browser.client.get("/api/v1/auth/session").status_code == 401
    # the request was not used up by the failure: the right response still works
    assert browser.post(browser.response(request_id, name_id=user.login), relay).status_code == 303
    # not base64
    assert browser.client.post(f"/api/v1/auth/saml/{browser.pid}/acs", data={"SAMLResponse": "%%%"}).status_code == 401


# -- pending requests: one use, bound to the browser ------------------------------------------------------------


def test_replayed_response_is_refused(app, idp: FakeIdp, make_provider, commit_env) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider(allow_signup=True)
    browser = Browser(app, idp, provider)
    email = f"{_uniq('replay')}@example.test"
    request_id, relay, _ = browser.start()
    response = browser.response(request_id, name_id=email, assertion_id="_replayed-assertion-" + uuid.uuid4().hex)
    assert browser.post(response, relay).status_code == 303
    # the same response again (the binding cookie is gone after a login)
    assert browser.post(response, relay).status_code == 401
    # the same response against a new login of the same browser: answers another request
    _new_request, new_relay, _ = browser.start()
    assert browser.post(response, new_relay).status_code == 401
    # a new response re-using the assertion id (replay cache)
    request_id2, relay2, _ = browser.start()
    reused = browser.response(request_id2, name_id=email, assertion_id=response.find(saml.T_ASSERTION).get("ID"))
    refused = browser.post(reused, relay2)
    assert refused.status_code == 401 and "already used" in refused.json()["message"]


def test_response_from_another_browser_is_refused(app, idp: FakeIdp, make_provider, commit_env) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider(allow_signup=True)
    victim = Browser(app, idp, provider)
    request_id, relay, _ = victim.start()
    response = victim.response(request_id, name_id=f"{_uniq('victim')}@example.test")
    attacker = TestClient(app, base_url="http://localhost")
    stolen = victim.post(response, relay, client=attacker)
    assert stolen.status_code == 401 and stolen.json()["message"] == "Login session expired; start again"
    # an attacker with a pending login of their own cannot use the victim's response either
    attacker_browser = Browser(app, idp, provider)
    attacker_browser.start()
    assert victim.post(response, relay, client=attacker_browser.client).status_code == 401
    # wrong RelayState
    assert victim.post(response, "tampered").status_code == 401
    assert victim.post(response, relay).status_code == 303


def test_unknown_and_used_in_response_to(system_env, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    provider = system_env["auth_saml.provider"].create(_provider_vals(idp, allow_signup=True))
    start = service.start_login(system_env, provider)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start.redirect_url).query))
    request_id = etree.fromstring(zlib.decompress(base64.b64decode(query["SAMLRequest"]), -15)).get("ID")

    def response(in_response_to: str) -> str:
        assertion = idp.assertion(
            request_id=in_response_to, acs_url=provider.acs(), audience=provider.entity_id(), name_id="u@example.test"
        )
        return encode(idp.response(assertion, acs_url=provider.acs(), request_id=in_response_to))

    with pytest.raises(saml.SamlError) as info:
        service.consume_response(
            system_env, provider, response("_never-issued"), query["RelayState"], start.binding_token
        )
    assert info.value.code == "in_response_to"
    with pytest.raises(saml.SamlError) as info:
        service.consume_response(system_env, provider, response(request_id), query["RelayState"], None)
    assert info.value.code == "no_binding"
    user, assertion = service.consume_response(
        system_env, provider, response(request_id), query["RelayState"], start.binding_token
    )
    assert user.login == "u@example.test" and assertion.in_response_to == request_id
    with pytest.raises(saml.SamlError) as info:  # one use
        service.consume_response(system_env, provider, response(request_id), query["RelayState"], start.binding_token)
    assert info.value.code == "no_pending_request"
    # a concurrent consumer used the request between the lookup and the update
    start2 = service.start_login(system_env, provider)
    query2 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start2.redirect_url).query))
    request_id2 = etree.fromstring(zlib.decompress(base64.b64decode(query2["SAMLRequest"]), -15)).get("ID")
    validate = saml.validate_response

    def racing(*args: Any, **kwargs: Any) -> Any:
        result = validate(*args, **kwargs)
        system_env.cr.execute("UPDATE auth_saml_request SET used = true")
        return result

    monkeypatch.setattr(saml, "validate_response", racing)
    with pytest.raises(saml.SamlError) as info:
        service.consume_response(
            system_env, provider, response(request_id2), query2["RelayState"], start2.binding_token
        )
    assert info.value.code == "request_used"


def test_expired_request_is_refused(system_env, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    provider = system_env["auth_saml.provider"].create(_provider_vals(idp, allow_signup=True))
    start = service.start_login(system_env, provider)
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(start.redirect_url).query))
    request_id = etree.fromstring(zlib.decompress(base64.b64decode(query["SAMLRequest"]), -15)).get("ID")
    assertion = idp.assertion(request_id=request_id, acs_url=provider.acs(), audience=provider.entity_id())
    xml = encode(idp.response(assertion, acs_url=provider.acs(), request_id=request_id))
    later = system_env.now() + service.REQUEST_TTL + dt.timedelta(seconds=1)
    with pytest.raises(saml.SamlError) as info:
        service.consume_response(system_env, provider, xml, query["RelayState"], start.binding_token, now=later)
    assert info.value.code == "no_pending_request"


# -- account mapping ---------------------------------------------------------------------------------------------


def _assertion(name_id: str, **attributes: str) -> saml.ValidatedAssertion:
    return saml.ValidatedAssertion(
        assertion_id="_a",
        issuer="idp",
        name_id=name_id,
        name_id_format=None,
        in_response_to="_r",
        valid_until=dt.datetime.now(dt.UTC),
        attributes={k: [v] for k, v in attributes.items()},
    )


def test_email_link_is_refused_unless_the_provider_is_trusted(system_env, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    untrusted = system_env["auth_saml.provider"].create(_provider_vals(idp, allow_signup=True))
    victim = system_env["core.user"].create({"login": f"{_uniq('victim')}@example.test", "name": "Victim"})
    with pytest.raises(AccessDenied) as info:
        service.user_for_assertion(system_env, untrusted, _assertion(victim.login))
    assert info.value.context["reason"] == "link_required"
    assert "ask an administrator to link it" in info.value.message
    assert not system_env["auth_saml.identity"].search([("user_id", "=", victim.ids[0])])
    trusted = system_env["auth_saml.provider"].create(_provider_vals(idp, trust_email=True))
    assert service.user_for_assertion(system_env, trusted, _assertion(victim.login.upper())) == victim
    link = system_env["auth_saml.identity"].search([("provider_id", "=", trusted.ids[0])])
    assert link.user_id == victim and link.subject == victim.login.upper()


def test_administrator_link_is_used_whatever_the_email(system_env, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    provider = system_env["auth_saml.provider"].create(_provider_vals(idp, email_attribute="mail"))
    user = system_env["core.user"].create({"login": _uniq("linked"), "name": "Linked"})
    system_env["auth_saml.identity"].create(
        {"provider_id": provider.ids[0], "subject": "E12345", "user_id": user.ids[0]}
    )
    assert service.user_for_assertion(system_env, provider, _assertion("E12345", mail="someone@else.test")) == user
    user.write({"active": False})
    with pytest.raises(AccessDenied) as info:
        service.user_for_assertion(system_env, provider, _assertion("E12345"))
    assert info.value.context["reason"] == "disabled"


def test_jit_signup_on_and_off(system_env, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    closed = system_env["auth_saml.provider"].create(_provider_vals(idp))
    email = f"{_uniq('new')}@example.test"
    with pytest.raises(AccessDenied) as info:
        service.user_for_assertion(system_env, closed, _assertion(email))
    assert info.value.context["reason"] == "no_account"
    assert not system_env["core.user"].search([("login", "=", email)])
    open_ = system_env["auth_saml.provider"].create(
        _provider_vals(idp, allow_signup=True, email_attribute="mail", name_attribute="cn")
    )
    user = service.user_for_assertion(system_env, open_, _assertion("P-1", mail=f"  {email.upper()} ", cn="New Person"))
    assert user.login == email and user.email == email and user.name == "New Person"
    assert user.group_ids == system_env.ref("core.group_user") and not user.share
    assert not user.has_group("core.group_system")
    # no usable e-mail address: no account
    with pytest.raises(AccessDenied) as info:
        service.user_for_assertion(system_env, open_, _assertion("P-2", mail="not-an-address"))
    assert info.value.context["reason"] == "no_email"


def test_cross_company_provider_visibility_and_scope(env, system_env, idp: FakeIdp, env_for) -> None:  # type: ignore[no-untyped-def]
    main = system_env.ref("core.main_company")
    other = system_env["core.company"].create({"name": _uniq("Subsidiary"), "currency_id": main.currency_id.ids[0]})
    scoped = system_env["auth_saml.provider"].create(_provider_vals(idp, company_id=other.ids[0], allow_signup=True))
    shared = system_env["auth_saml.provider"].create(_provider_vals(idp))
    # an administrator of the main company only does not see the subsidiary's provider
    local_admin = system_env["core.user"].create(
        {
            "login": _uniq("localadmin"),
            "name": "Local admin",
            "company_id": main.ids[0],
            "company_ids": [("set", main.ids)],
            "group_ids": [("set", system_env.ref("core.group_system").ids)],
        }
    )
    seen = env.with_user(local_admin.ids[0])["auth_saml.provider"].search(
        [("id", "in", [scoped.ids[0], shared.ids[0]])]
    )
    assert seen.ids == shared.ids
    with pytest.raises(AccessError):
        env.with_user(local_admin.ids[0])["auth_saml.provider"].create(_provider_vals(idp, company_id=other.ids[0]))
    # the subsidiary's provider does not sign in people of the main company…
    employee = system_env["core.user"].create(
        {"login": f"{_uniq('emp')}@example.test", "name": "Employee", "company_id": main.ids[0]}
    )
    system_env["auth_saml.identity"].create(
        {"provider_id": scoped.ids[0], "subject": "emp", "user_id": employee.ids[0]}
    )
    with pytest.raises(AccessDenied) as info:
        service.user_for_assertion(system_env, scoped, _assertion("emp"))
    assert info.value.context["reason"] == "company"
    # …and creates its new accounts in the subsidiary
    newcomer = service.user_for_assertion(system_env, scoped, _assertion(f"{_uniq('sub')}@example.test"))
    assert newcomer.company_id == other and newcomer.company_ids == other


# -- administration -----------------------------------------------------------------------------------------------


def test_providers_are_for_administrators_only(make_provider, reader: Session, admin: Session) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider()
    assert reader.get("/api/v1/models/auth_saml.provider").status_code == 403
    assert reader.get(f"/api/v1/models/auth_saml.provider/{provider.ids[0]}").status_code == 403
    assert reader.get("/api/v1/models/auth_saml.identity").status_code == 403
    assert reader.post("/api/v1/models/auth_saml.provider", json={"values": {"name": "x"}}).status_code == 403
    assert admin.get(f"/api/v1/models/auth_saml.provider/{provider.ids[0]}").status_code == 200
    menus = admin.get("/api/v1/web/menus").json()
    names = []
    stack = list(menus)
    while stack:
        node = stack.pop()
        names.append(node["name"])
        stack.extend(node.get("children") or [])
    assert "SAML providers" in names and "SAML account links" in names
    views = admin.get("/api/v1/web/views", params={"model": "auth_saml.provider", "types": "list,form"})
    assert views.status_code == 200, views.text


def test_provider_validation(system_env, idp: FakeIdp, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    providers = system_env["auth_saml.provider"]
    with pytest.raises(ValidationError):
        providers.create(
            _provider_vals(idp, idp_certificates="-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----")
        )
    with pytest.raises(ValidationError):
        providers.create(_provider_vals(idp, idp_certificates="   "))
    with pytest.raises(ValidationError):
        providers.create(_provider_vals(idp, idp_sso_url="javascript:alert(1)"))
    with pytest.raises(ValidationError):
        providers.create(_provider_vals(idp, sign_requests=True))  # no key
    key_pem, cert_pem = saml.generate_sp_keypair("SP")
    _, other_cert = saml.generate_sp_keypair("other")
    with pytest.raises(ValidationError):
        providers.create(_provider_vals(idp, sign_requests=True, sp_private_key=key_pem, sp_certificate=other_cert))
    with pytest.raises(ValidationError):
        providers.create(_provider_vals(idp, sign_requests=True, sp_private_key="garbage", sp_certificate=cert_pem))
    with pytest.raises(ValidationError):
        providers.create(_provider_vals(idp, sp_certificate="not a certificate"))
    with pytest.raises(Exception):  # noqa: B017 - CHECK constraint
        with system_env.cr.savepoint():
            providers.create(_provider_vals(idp, clock_skew=3600))
            system_env.flush()
    ok = providers.create(_provider_vals(idp, sign_requests=True, sp_private_key=key_pem, sp_certificate=cert_pem))
    assert ok.private_key() is not None and ok.signing_certificate() is not None
    # several certificates (key rotation)
    rotating = providers.create(_provider_vals(idp, idp_certificates=idp.cert_pem + new_identity("next")[2]))
    assert len(service.sp_config(rotating).idp_certificates) == 2
    # production: https only
    assert providers.create(_provider_vals(idp, idp_sso_url="http://idp.example.test/sso"))  # development
    monkeypatch.setattr(get_settings(), "env", "production")
    with pytest.raises(ValidationError):
        providers.create(_provider_vals(idp, idp_sso_url="http://idp.example.test/sso"))


def test_purge_job(system_env, idp: FakeIdp) -> None:  # type: ignore[no-untyped-def]
    provider = system_env["auth_saml.provider"].create(_provider_vals(idp))
    past = system_env.now() - dt.timedelta(hours=1)
    future = system_env.now() + dt.timedelta(hours=1)
    requests = system_env["auth_saml.request"]
    common = {"provider_id": provider.ids[0], "binding_hash": "b", "relay_hash": "r"}
    expired = requests.create({**common, "request_id": _uniq("_e"), "expires_at": past})
    used = requests.create({**common, "request_id": _uniq("_u"), "expires_at": future, "used": True})
    pending = requests.create({**common, "request_id": _uniq("_p"), "expires_at": future})
    seen = system_env["auth_saml.assertion"]
    old = seen.create({"provider_id": provider.ids[0], "assertion_id": "_old", "expires_at": past})
    fresh = seen.create({"provider_id": provider.ids[0], "assertion_id": "_fresh", "expires_at": future})
    counts = service.purge_expired(system_env)
    assert counts["requests"] >= 2 and counts["assertions"] >= 1
    assert not (expired | used).exists() and pending.exists()
    assert not old.exists() and fresh.exists()


# -- request pipeline: CSRF, cookies, rate limit, second factor ------------------------------------------------


def test_assertion_consumer_ignores_a_stale_session_without_csrf(
    app, idp: FakeIdp, make_provider, admin: Session
) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider()
    # an identity provider on the same site posts the form with the session cookie of a signed-in user
    posted = admin.client.post(f"/api/v1/auth/saml/{provider.ids[0]}/acs", data={"SAMLResponse": "eA=="})
    assert posted.status_code == 401, posted.text  # treated as anonymous and refused for its content, not CSRF
    assert "CSRF" not in posted.json()["message"]
    # other unsafe requests still need the token
    assert (
        admin.client.post("/api/v1/auth/password", json={"current_password": "x", "new_password": "y" * 8}).status_code
        == 403
    )


def test_binding_cookie_is_cross_site_in_production(app, make_provider, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider()
    anon = TestClient(app, base_url="http://localhost")
    dev = anon.get(f"/api/v1/auth/saml/{provider.ids[0]}/login", follow_redirects=False).headers["set-cookie"]
    assert "samesite=lax" in dev.lower() and "Path=/api/v1/auth/saml/" in dev and "HttpOnly" in dev
    monkeypatch.setattr(get_settings(), "cookie_secure", True)
    prod = anon.get(f"/api/v1/auth/saml/{provider.ids[0]}/login", follow_redirects=False).headers["set-cookie"]
    assert "samesite=none" in prod.lower() and "Secure" in prod


def test_assertion_consumer_shares_the_login_rate_limit(app, make_provider) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider()
    limiter._hits.clear()
    anon = TestClient(app, base_url="http://localhost")
    limit = get_settings().login_rate_limit_per_minute
    codes = [
        anon.post(f"/api/v1/auth/saml/{provider.ids[0]}/acs", data={"SAMLResponse": "eA=="}).status_code
        for _ in range(limit + 1)
    ]
    limiter._hits.clear()
    assert codes[:limit] == [401] * limit and codes[-1] == 429


def test_totp_user_signs_in_without_a_local_second_factor(app, idp: FakeIdp, make_provider, commit_env) -> None:  # type: ignore[no-untyped-def]
    """Like OIDC, the identity provider is trusted for the authentication strength."""
    provider = make_provider()
    user = _committed_user(commit_env)
    user.elevated().with_context(_recompute_write=True)._write_columns(
        {"totp_secret": pyotp.random_base32(), "totp_enabled": True}
    )
    commit_env["auth_saml.identity"].create(
        {"provider_id": provider.ids[0], "subject": "totp-user", "user_id": user.ids[0]}
    )
    commit_env.cr.commit()
    browser = Browser(app, idp, provider)
    assert browser.login("totp-user").status_code == 303
    session = commit_env["core.session"].search([("user_id", "=", user.ids[0]), ("revoked", "=", False)], limit=1)
    assert session and session.totp_verified and not session.pre_auth


def test_account_refusal_is_reported(app, idp: FakeIdp, make_provider, commit_env) -> None:  # type: ignore[no-untyped-def]
    provider = make_provider()  # no sign-up, no e-mail trust
    user = _committed_user(commit_env)
    refused = Browser(app, idp, provider).login(user.login)
    assert refused.status_code == 401 and refused.json()["reason"] == "link_required"
