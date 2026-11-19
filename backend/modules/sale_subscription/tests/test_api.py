"""Customer portal endpoints: own subscriptions only, status and next invoice, token links, closing when the
plan allows it (at the period end, with a customer reason)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tessera.api.tests.conftest import Session
from tessera.config import get_settings
from tessera.orm.environment import SYSTEM_UID, environment_for

pytestmark = pytest.mark.db
PASSWORD = "portal-pass-123"


@pytest.fixture
def world(app: Any) -> dict[str, Any]:
    """Two customers with a portal user and a running subscription each (committed)."""
    from modules.portal.models.access import accept_invitation
    from modules.sale_subscription import billing

    tag = uuid.uuid4().hex[:6]
    out: dict[str, Any] = {}
    with environment_for(get_settings().database_url, SYSTEM_UID, su=True) as env:
        closable = env["sale.subscription_plan"].create(
            {"name": f"Closable {tag}", "user_closable": True, "auto_close_limit": 0}
        )
        locked = env["sale.subscription_plan"].create({"name": f"Locked {tag}", "auto_close_limit": 0})
        product = env["product.product"].create(
            {"name": f"Portal seat {tag}", "list_price": 25, "type": "service", "recurring_invoice": True}
        )
        for key, the_plan in (("a", closable), ("b", locked)):
            company = env["contacts.partner"].create({"name": f"Sub cust {key} {tag}", "is_company": True})
            person = env["contacts.partner"].create(
                {
                    "name": f"Sub buyer {key} {tag}",
                    "parent_id": company.ids[0],
                    "email": f"sub-{key}-{tag}@example.test",
                }
            )
            invite = person.action_grant_portal()
            accept_invitation(env, invite["invitation_url"].split("token=")[1], PASSWORD)
            sub = env["sale.quotation"].create(
                {
                    "partner_id": person.ids[0],
                    "plan_id": the_plan.ids[0],
                    "line_ids": [("create", {"product_id": product.ids[0], "quantity": 2})],
                }
            )
            sub.action_confirm()
            billing.invoice_due_periods(sub, env.today())
            sub.write({"access_token": f"tok-{key}-{tag}"})
            out[key] = {"login": f"sub-{key}-{tag}@example.test", "id": sub.ids[0], "token": f"tok-{key}-{tag}"}
        out["reasons"] = {r.ids[0]: r.customer_visible for r in env["sale.subscription_close_reason"].search([])}
    return out


def test_portal_user_sees_and_closes_own_subscription(app: Any, world: dict[str, Any]) -> None:
    session = Session(TestClient(app, base_url="http://localhost"), world["a"]["login"], PASSWORD)
    listing = session.get("/api/v1/sale/subscriptions").json()["subscriptions"]
    assert [s["id"] for s in listing] == [world["a"]["id"]]
    assert listing[0]["state"] == "progress" and listing[0]["recurring_total"] == 50
    detail = session.get(f"/api/v1/sale/subscriptions/{world['a']['id']}").json()
    assert detail["can_close"] and detail["next_invoice_date"] and detail["plan"].startswith("Closable")
    assert detail["state_label"] == "In progress" and detail["lines"][0]["quantity"] == 2
    assert len(detail["invoices"]) == 1 and detail["invoices"][0]["url"].startswith("/portal/invoices/")
    visible = {r["id"] for r in detail["close_reasons"]}
    assert visible == {rid for rid, shown in world["reasons"].items() if shown}
    assert session.get(f"/api/v1/sale/subscriptions/{world['b']['id']}").status_code == 404
    hidden = next(rid for rid, shown in world["reasons"].items() if not shown)
    bad = session.post(f"/api/v1/sale/subscriptions/{world['a']['id']}/close", json={"reason_id": hidden})
    assert bad.status_code == 400
    closed = session.post(
        f"/api/v1/sale/subscriptions/{world['a']['id']}/close",
        json={"reason_id": next(iter(visible)), "note": "Budget cuts"},
    )
    assert closed.status_code == 200, closed.text
    body = closed.json()
    assert body["closing"] and not body["can_close"] and body["end_date"] and body["state"] == "progress"
    again = session.post(
        f"/api/v1/sale/subscriptions/{world['a']['id']}/close", json={"reason_id": next(iter(visible))}
    )
    assert again.status_code == 400


def test_token_links_and_plans_that_forbid_closing(client: TestClient, admin: Any, world: dict[str, Any]) -> None:
    b = world["b"]
    url = f"/api/v1/sale/subscriptions/{b['id']}"
    assert client.get(url).status_code == 401
    assert client.get(url, params={"token": "wrong"}).status_code == 401
    view = client.get(url, params={"token": b["token"]}).json()
    assert view["id"] == b["id"] and not view["can_close"] and view["close_reasons"] == []
    refused = client.post(f"{url}/close", json={"reason_id": 1, "token": b["token"]})
    assert refused.status_code == 400
    assert client.get("/api/v1/sale/subscriptions").status_code == 401
    assert admin.get(url).json()["id"] == b["id"]  # internal users with read access
    assert admin.get("/api/v1/sale/subscriptions/999999999").status_code == 404
