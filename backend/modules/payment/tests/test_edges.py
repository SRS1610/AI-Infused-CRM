"""Edge cases of the payment flows: provider errors, sync on return, refusals, hooks."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from modules.account.tests.common import eu_env, invoice, partner
from modules.payment import service
from modules.payment.hooks import install_providers, post_init
from modules.payment.providers import ProviderError, ProviderEvent, adapter_for, register_adapter
from modules.payment.providers import stripe as stripe_mod
from modules.payment.providers.testing import TestAdapter
from tessera.exceptions import UserError

pytestmark = pytest.mark.db


def _provider(env: Any, code: str = "test", **vals: Any) -> Any:
    providers = env["payment.provider"].search([("company_id", "=", env.company.ids[0])])
    provider = env.ref(f"payment.{env.company.ids[0]}_provider_{code}")
    (providers - provider).write({"state": "disabled"})
    provider.write({"state": "test", **vals})
    return provider


def test_checkout_failure_keeps_message(env, monkeypatch):  # type: ignore[no-untyped-def]
    provider = _provider(env, "stripe", secret_key="sk_test_x", webhook_secret="whsec_x")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Amount must be at least 0.50"}})

    monkeypatch.setattr(stripe_mod, "transport_factory", lambda: httpx.MockTransport(handler))
    inv = invoice(env, partner(env, "Too Small"), [{"price_unit": 0.1}])
    tx = service.create_invoice_transaction(inv, provider)
    with pytest.raises(UserError):
        service.start_checkout(tx, "https://app.test/r")
    assert tx.state == "error" and "0.50" in tx.state_message


def test_return_sync_settles_pending_transaction(env, monkeypatch):  # type: ignore[no-untyped-def]
    provider = _provider(env, "stripe", secret_key="sk_test_x", webhook_secret="whsec_x")
    session: dict[str, Any] = {"id": "cs_sync_1", "url": "https://checkout.stripe.test/x"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json=session)
        return httpx.Response(
            200,
            json={
                **session,
                "payment_status": "paid",
                "amount_total": 4500,
                "currency": "usd",
                "client_reference_id": tx.reference,
                "payment_intent": "pi_sync",
            },
        )

    monkeypatch.setattr(stripe_mod, "transport_factory", lambda: httpx.MockTransport(handler))
    inv = invoice(env, partner(env, "Returning Buyer"), [{"price_unit": 45.0}])
    tx = service.create_invoice_transaction(inv, provider)
    service.start_checkout(tx, "https://app.test/r")
    assert tx.state == "pending"
    assert tx.action_sync() == "done" and inv.payment_state == "paid"
    assert service.sync_transaction(tx).state == "done"  # final: nothing asked anymore


def test_refusals(env):  # type: ignore[no-untyped-def]
    provider = _provider(env)
    a = partner(env, "Refusal A")
    b = partner(env, "Refusal B")
    inv_a = invoice(env, a, [{"price_unit": 10.0}])
    inv_b = invoice(env, b, [{"price_unit": 10.0}])
    with pytest.raises(UserError):
        service.create_invoice_transaction(inv_a | inv_b)  # two customers
    draft = invoice(env, a, [{"price_unit": 10.0}], post=False)
    with pytest.raises(UserError):
        service.create_invoice_transaction(draft)
    e = eu_env(env)
    eu_inv = invoice(e, partner(e, "EU Refusal"), [{"price_unit": 10.0}])
    with pytest.raises(UserError):
        service.create_invoice_transaction(inv_a | eu_inv)  # two companies
    provider.write({"state": "disabled"})
    with pytest.raises(UserError):
        service.create_invoice_transaction(inv_a)  # nothing enabled
    with pytest.raises(UserError):
        service.create_invoice_transaction(inv_a, provider)  # disabled provider
    tx = env["payment.transaction"].create(
        {
            "reference": "EDGE-1",
            "provider_id": provider.ids[0],
            "amount": 1.0,
            "currency_id": env.company.currency_id.ids[0],
        }
    )
    tx.unlink()  # drafts can be deleted
    provider.write({"state": "test"})
    paid = service.create_invoice_transaction(inv_a)
    service.start_checkout(paid, "https://app.test/r")
    with pytest.raises(UserError):
        paid.unlink()


def test_provider_errors_on_token_and_refund(env, monkeypatch):  # type: ignore[no-untyped-def]
    provider = _provider(env)
    customer = partner(env, "Error Buyer")
    token = env["payment.token"].create(
        {"provider_id": provider.ids[0], "partner_id": customer.ids[0], "provider_ref": "tok", "display": "•••• 1111"}
    )

    def boom(self: Any, *args: Any) -> Any:
        raise ProviderError("service down")

    monkeypatch.setattr(TestAdapter, "charge_token", boom)
    tx = service.charge_token(token, 3.0)
    assert tx.state == "error" and tx.state_message == "service down"
    inv = invoice(env, customer, [{"price_unit": 20.0}])
    done = service.create_invoice_transaction(inv)
    service.start_checkout(done, "https://app.test/r")
    monkeypatch.setattr(TestAdapter, "refund", boom)
    with pytest.raises(UserError):
        service.refund_transaction(done)

    def refused(self: Any, tx: Any, amount: float) -> ProviderEvent:
        return ProviderEvent(event_id="r", event_type="refund", state="error", message="Refund declined")

    monkeypatch.setattr(TestAdapter, "refund", refused)
    refund = service.refund_transaction(done, 5.0)
    assert refund.state == "error" and refund.state_message == "Refund declined"


def test_pending_and_authorized_events(env):  # type: ignore[no-untyped-def]
    provider = _provider(env)
    inv = invoice(env, partner(env, "Slow Bank Buyer"), [{"price_unit": 30.0}])
    tx = service.create_invoice_transaction(inv)
    tx.write({"provider_reference": "pi_slow"})
    tx._set_state("pending")
    event = ProviderEvent(event_id="auth-1", event_type="x", state="authorized", provider_reference="pi_slow")
    assert service.process_event(provider, event) == "processed" and tx.state == "authorized"
    ignored = ProviderEvent(event_id="ign-1", event_type="x", state="ignored", reference=tx.reference)
    assert service.process_event(provider, ignored) == "ignored"
    refund_note = ProviderEvent(event_id="ref-1", event_type="x", state="refunded", reference=tx.reference)
    assert service.process_event(provider, refund_note) == "ignored"
    wrong_currency = ProviderEvent(
        event_id="cur-1", event_type="x", state="done", reference=tx.reference, amount=30.0, currency="JPY"
    )
    service.process_event(provider, wrong_currency)
    assert tx.state == "error" and "JPY" in tx.state_message


def test_adapter_registry_and_hooks(env):  # type: ignore[no-untyped-def]
    provider = _provider(env)
    assert isinstance(adapter_for(provider), TestAdapter)
    register_adapter("test", TestAdapter)
    fake = type("P", (), {"code": "nope"})()
    with pytest.raises(ProviderError):
        adapter_for(fake)
    assert not install_providers(env.company)  # idempotent
    post_init(env)
    assert env["payment.provider"]._available(env.company)


def test_webhook_errors_over_http(client, admin):  # type: ignore[no-untyped-def]
    from modules.payment.providers.stripe import sign
    from modules.payment.providers.testing import DEFAULT_SECRET

    found = admin.get(
        "/api/v1/models/payment.provider",
        params={"domain": json.dumps([["code", "=", "test"], ["company_id.name", "=", "My Company"]]), "fields": "id"},
    ).json()
    provider_id = found["items"][0]["id"]
    admin.patch(f"/api/v1/models/payment.provider/{provider_id}", json={"values": {"state": "test"}})
    body = b"not json"
    res = client.post(
        f"/api/v1/payment/webhook/{provider_id}", content=body, headers={"Test-Signature": sign(body, DEFAULT_SECRET)}
    )
    assert res.status_code == 400
    unknown = json.dumps({"id": "edge-unknown-evt", "state": "done", "reference": "NOPE"}).encode()
    res = client.post(
        f"/api/v1/payment/webhook/{provider_id}",
        content=unknown,
        headers={"Test-Signature": sign(unknown, DEFAULT_SECRET)},
    )
    assert res.status_code == 200 and res.json()["results"][0] in ("unknown", "duplicate")
    admin.patch(f"/api/v1/models/payment.provider/{provider_id}", json={"values": {"state": "disabled"}})
    assert client.post(f"/api/v1/payment/webhook/{provider_id}", content=unknown).status_code == 404
