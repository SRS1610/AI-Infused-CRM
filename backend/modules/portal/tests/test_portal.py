"""Portal (1.4-n): invitation, own documents only, no back-office API."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera.api.tests.conftest import Session
from tessera.config import get_settings
from tessera.orm.environment import SYSTEM_UID, environment_for

pytestmark = pytest.mark.db


def test_invitation_lifecycle(env: Any) -> None:
    from modules.portal.models.access import accept_invitation
    from tessera.exceptions import UserError

    partner = env["contacts.partner"].create({"name": "Portal Pam", "email": "Pam@Customer.example"})
    out = partner.action_grant_portal()
    token = out["invitation_url"].split("token=")[1]
    user = env["core.user"].with_context(active_test=False).browse([out["user_id"]])
    assert user.share and not user.active and user.login == "pam@customer.example" and user.partner_id == partner
    mail = env["core.mail_queue"].elevated().search([("email_to", "=", "pam@customer.example")], order="id desc", limit=1)
    assert "portal/invite?token=" in mail.body_html
    with pytest.raises(UserError, match="at least 8"):
        accept_invitation(env, token, "short")
    accept_invitation(env, token, "a-good-password")
    assert user.active and partner.has_portal_access
    with pytest.raises(UserError, match="already used"):
        accept_invitation(env, token, "a-good-password")
    partner.action_revoke_portal()
    assert not user.active and not partner.has_portal_access


def test_internal_contacts_cannot_be_invited(env: Any) -> None:
    from tessera.exceptions import UserError

    admin_partner = env.ref("core.user_admin").partner_id
    admin_partner.write({"email": "admin@yourcompany.example"})
    with pytest.raises(UserError, match="internal user"):
        admin_partner.action_grant_portal()


@pytest.fixture
def portal_world(app: Any) -> Any:
    """Two customer companies, each with a portal user and a sent quotation (committed)."""
    url = get_settings().database_url
    tag = uuid.uuid4().hex[:6]
    with environment_for(url, SYSTEM_UID, su=True) as env:
        out = {}
        for key in ("a", "b"):
            company = env["contacts.partner"].create({"name": f"Cust {key.upper()} {tag}", "is_company": True})
            person = env["contacts.partner"].create({"name": f"Buyer {key} {tag}", "parent_id": company.ids[0], "email": f"buyer-{key}-{tag}@example.test"})
            invite = person.action_grant_portal()
            from modules.portal.models.access import accept_invitation

            accept_invitation(env, invite["invitation_url"].split("token=")[1], "portal-pass-123")
            quote = env["sale.quotation"].create({"partner_id": person.ids[0], "line_ids": [("create", {"name": "Item", "price_unit": 10})]})
            quote.action_send()
            draft = env["sale.quotation"].create({"partner_id": company.ids[0], "line_ids": [("create", {"name": "Draft item", "price_unit": 5})]})
            out[key] = {"login": f"buyer-{key}-{tag}@example.test", "quote": quote.ids[0], "draft": draft.ids[0], "partner": person.ids[0]}
    return out


def test_portal_user_sees_only_own_documents_and_no_backoffice(app: Any, portal_world: Any) -> None:
    """1.4-n"""
    session = Session(TestClient(app, base_url="http://localhost"), portal_world["a"]["login"], "portal-pass-123")
    docs = session.get("/api/v1/portal/documents").json()["quotations"]
    assert [d["id"] for d in docs] == [portal_world["a"]["quote"]]  # not B's, not the draft
    assert session.get(f"/api/v1/sale/quotes/{portal_world['a']['quote']}").json()["state"] == "sent"
    assert session.get(f"/api/v1/sale/quotes/{portal_world['b']['quote']}").status_code == 404
    assert session.get(f"/api/v1/sale/quotes/{portal_world['a']['draft']}").status_code == 404
    # the back-office API and its method calls are closed to portal users
    assert session.get("/api/v1/models/sale.quotation").status_code == 403
    assert session.post(f"/api/v1/models/sale.quotation/{portal_world['a']['quote']}/call/action_cancel", json={}).status_code == 403
    assert session.post("/api/v1/jsonrpc", json={"jsonrpc": "2.0", "id": 1, "method": "search_read", "params": {"model": "sale.quotation"}}).status_code in (401, 403) or "error" in session.post("/api/v1/jsonrpc", json={"jsonrpc": "2.0", "id": 1, "method": "search_read", "params": {"model": "sale.quotation"}}).json()
    # profile: own contact only, whitelisted fields
    me = session.get("/api/v1/portal/me").json()
    assert me["name"].startswith("Buyer a") and me["company"].startswith("Cust A")
    updated = session.patch("/api/v1/portal/me", json={"phone": "+44 20 0000 0000", "city": "Leeds"}).json()
    assert updated["phone"] == "+44 20 0000 0000" and updated["city"] == "Leeds"
    assert session.patch("/api/v1/portal/me", json={"email": "evil@example.test"}).json()["email"] != "evil@example.test"
    # accepting through the session (no token needed)
    accepted = session.post(f"/api/v1/sale/quotes/{portal_world['a']['quote']}/accept", json={"name": "Buyer A"}).json()
    assert accepted["state"] == "accepted"


def test_invite_accept_endpoint(client: Any) -> None:
    url = get_settings().database_url
    with environment_for(url, SYSTEM_UID, su=True) as env:
        partner = env["contacts.partner"].create({"name": "Endpoint Ed", "email": f"ed-{uuid.uuid4().hex[:6]}@example.test"})
        token = partner.action_grant_portal()["invitation_url"].split("token=")[1]
    assert client.post("/api/v1/portal/invite/accept", json={"token": "bogus", "password": "whatever-123"}).status_code == 400
    ok = client.post("/api/v1/portal/invite/accept", json={"token": token, "password": "whatever-123"})
    assert ok.status_code == 200 and ok.json()["login"].startswith("ed-")
    login = client.post("/api/v1/auth/login", json={"login": ok.json()["login"], "password": "whatever-123"})
    assert login.status_code == 200 and login.json()["user"]["share"] is True
