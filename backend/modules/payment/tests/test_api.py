"""Portal payment through the API (2-j): pay from the emailed link, Stripe webhook, replay is a no-op."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from modules.payment.providers import stripe as stripe_mod
from modules.payment.providers.stripe import sign

pytestmark = pytest.mark.db
WHSEC = "whsec_api_test"


def _invoice(admin: Any, amount: float) -> tuple[int, str]:
    name = f"Portal payer {uuid.uuid4().hex[:6]}"
    p = admin.post(
        "/api/v1/models/contacts.partner", json={"values": {"name": name, "email": "payer@example.test"}}
    ).json()
    move = admin.post(
        "/api/v1/models/account.move",
        json={
            "values": {
                "move_type": "out_invoice",
                "partner_id": p["id"],
                "invoice_line_ids": [["create", {"name": "Service", "price_unit": amount, "quantity": 1}]],
            }
        },
    ).json()
    admin.post(f"/api/v1/models/account.move/{move['id']}/call/action_post", json={})
    sent = admin.post(f"/api/v1/models/account.move/{move['id']}/call/action_send", json={}).json()["result"]
    return move["id"], sent["portal_url"].split("token=")[1]


def _provider_id(admin: Any, code: str) -> int:
    found = admin.get(
        "/api/v1/models/payment.provider",
        params={"domain": json.dumps([["code", "=", code], ["company_id.name", "=", "My Company"]]), "fields": "id"},
    ).json()
    return int(found["items"][0]["id"])


def test_pay_with_test_provider_from_link(client, admin):  # type: ignore[no-untyped-def]
    provider_id = _provider_id(admin, "test")
    for pid in (provider_id, _provider_id(admin, "stripe")):
        admin.patch(f"/api/v1/models/payment.provider/{pid}", json={"values": {"state": "disabled"}})
    move_id, token = _invoice(admin, 75.0)
    options = client.get(f"/api/v1/payment/invoices/{move_id}", params={"token": token}).json()
    assert options["can_pay"] is False and options["providers"] == []  # nothing enabled
    assert client.post(f"/api/v1/payment/invoices/{move_id}/pay", json={"token": token}).status_code == 400
    admin.patch(f"/api/v1/models/payment.provider/{provider_id}", json={"values": {"state": "test"}})
    options = client.get(f"/api/v1/payment/invoices/{move_id}", params={"token": token}).json()
    assert options["can_pay"] is True and options["providers"][0]["code"] == "test"
    assert client.post(f"/api/v1/payment/invoices/{move_id}/pay", json={"token": "wrong"}).status_code == 401
    res = client.post(f"/api/v1/payment/invoices/{move_id}/pay", json={"token": token, "provider_id": provider_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["state"] == "done" and f"/portal/invoices/{move_id}?payment=" in body["redirect_url"]
    status = client.get(
        f"/api/v1/payment/invoices/{move_id}/status", params={"token": token, "reference": body["reference"]}
    ).json()
    assert status["state"] == "done" and status["payment_state"] == "paid"
    assert (
        client.get(
            f"/api/v1/payment/invoices/{move_id}/status", params={"token": token, "reference": "NOPE"}
        ).status_code
        == 404
    )
    again = client.get(f"/api/v1/payment/invoices/{move_id}", params={"token": token}).json()
    assert again["can_pay"] is False and again["transactions"][0]["state"] == "done"
    admin.patch(f"/api/v1/models/payment.provider/{provider_id}", json={"values": {"state": "disabled"}})


def test_stripe_portal_payment_and_webhook_replay(client, admin, monkeypatch):  # type: ignore[no-untyped-def]
    """2-j: portal payment through the Stripe adapter creates a payment and reconciles the invoice."""
    session_id = f"cs_{uuid.uuid4().hex[:10]}"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.stripe.com"
        return httpx.Response(200, json={"id": session_id, "url": f"https://checkout.stripe.test/{session_id}"})

    monkeypatch.setattr(stripe_mod, "transport_factory", lambda: httpx.MockTransport(handler))
    provider_id = _provider_id(admin, "stripe")
    admin.patch(
        f"/api/v1/models/payment.provider/{provider_id}",
        json={"values": {"state": "test", "secret_key": "sk_test_api", "webhook_secret": WHSEC}},
    )
    move_id, token = _invoice(admin, 130.0)
    res = client.post(f"/api/v1/payment/invoices/{move_id}/pay", json={"token": token, "provider_id": provider_id})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["redirect_url"].endswith(session_id) and body["state"] == "pending"
    event_id = f"evt_{uuid.uuid4().hex[:10]}"
    payload = json.dumps(
        {
            "id": event_id,
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": session_id,
                    "client_reference_id": body["reference"],
                    "payment_status": "paid",
                    "amount_total": 13000,
                    "currency": "usd",
                    "payment_intent": f"pi_{event_id}",
                }
            },
        }
    ).encode()
    bad = client.post(
        f"/api/v1/payment/webhook/{provider_id}", content=payload, headers={"Stripe-Signature": sign(payload, "nope")}
    )
    assert bad.status_code == 401
    headers = {"Stripe-Signature": sign(payload, WHSEC), "Content-Type": "application/json"}
    first = client.post(f"/api/v1/payment/webhook/{provider_id}", content=payload, headers=headers)
    assert first.status_code == 200 and first.json()["results"] == ["processed"]
    replay = client.post(f"/api/v1/payment/webhook/{provider_id}", content=payload, headers=headers)
    assert replay.status_code == 200 and replay.json()["results"] == ["duplicate"]
    invoice = admin.get(
        f"/api/v1/models/account.move/{move_id}", params={"fields": "payment_state,amount_residual"}
    ).json()
    assert invoice["payment_state"] in ("paid", "in_payment") and invoice["amount_residual"] == 0
    payments = admin.get(
        "/api/v1/models/account.payment",
        params={"domain": json.dumps([["ref", "=", body["reference"]]]), "fields": "id"},
    ).json()
    assert payments["total"] == 1
    assert client.post("/api/v1/payment/webhook/999999", content=payload, headers=headers).status_code == 404
    admin.patch(f"/api/v1/models/payment.provider/{provider_id}", json={"values": {"state": "disabled"}})
