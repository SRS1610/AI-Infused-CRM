"""Stripe adapter over a mocked transport (no request leaves the process) and webhook signatures."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from modules.payment.providers import ProviderError, SignatureError
from modules.payment.providers import stripe as stripe_mod
from modules.payment.providers.base import from_minor, to_minor
from modules.payment.providers.stripe import StripeAdapter, sign, verify_signature

SECRET = "whsec_test_123"


class FakeProvider:
    code = "stripe"

    def __init__(self, secrets: dict[str, str]) -> None:
        self.secrets = secrets

    def _secret(self, name: str) -> str | None:
        return self.secrets.get(name)


def _tx(**kw: Any) -> Any:
    defaults = {
        "reference": "INV/2026/00001-ABC",
        "amount": 120.5,
        "currency_id": SimpleNamespace(name="EUR"),
        "partner_id": SimpleNamespace(email="buyer@example.test"),
        "tokenize": False,
        "provider_reference": "pi_123",
    }
    return SimpleNamespace(**{**defaults, **kw})


@pytest.fixture
def stripe_calls(monkeypatch):  # type: ignore[no-untyped-def]
    calls: list[httpx.Request] = []
    responses: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        key = f"{request.method} {request.url.path}"
        status, body = responses.get(key, (404, {"error": {"message": "not mocked"}}))
        return httpx.Response(status, json=body)

    monkeypatch.setattr(stripe_mod, "transport_factory", lambda: httpx.MockTransport(handler))
    return calls, responses


def test_signature_verification():
    body = b'{"id": "evt_1"}'
    now = 1_800_000_000
    header = sign(body, SECRET, now)
    assert verify_signature(header, body, SECRET, now=now + 10) == now
    assert verify_signature(f"{header},v1=deadbeef", body, SECRET, now=now) == now  # several signatures
    with pytest.raises(SignatureError):
        verify_signature(header, body + b" ", SECRET, now=now)  # tampered body
    with pytest.raises(SignatureError):
        verify_signature(header, body, "whsec_other", now=now)
    with pytest.raises(SignatureError):
        verify_signature(header, body, SECRET, now=now + 301)  # replayed after 5 minutes
    for bad in (None, "", "t=abc,v1=x", f"t={now}", "v1=abc"):
        with pytest.raises(SignatureError):
            verify_signature(bad, body, SECRET, now=now)


def test_minor_units():
    assert to_minor(12.34, "eur") == 1234 and to_minor(500, "JPY") == 500
    assert from_minor(1234, "EUR") == 12.34 and from_minor(None, "EUR") is None


def test_checkout_session(stripe_calls):  # type: ignore[no-untyped-def]
    calls, responses = stripe_calls
    responses["POST /v1/checkout/sessions"] = (
        200,
        {"id": "cs_test_1", "url": "https://checkout.stripe.test/c/cs_test_1"},
    )
    adapter = StripeAdapter(FakeProvider({"secret_key": "sk_test_x", "webhook_secret": SECRET}))
    result = adapter.create_checkout(_tx(tokenize=True), "https://app.test/return", "https://app.test/cancel")
    assert result.redirect_url.startswith("https://checkout.stripe.test/") and result.provider_reference == "cs_test_1"
    sent = parse_qs(calls[0].content.decode())
    assert sent["line_items[0][price_data][unit_amount]"] == ["12050"]
    assert sent["line_items[0][price_data][currency]"] == ["eur"]
    assert sent["client_reference_id"] == ["INV/2026/00001-ABC"]
    assert sent["payment_intent_data[setup_future_usage]"] == ["off_session"]
    assert calls[0].headers["authorization"].startswith("Basic ")
    assert calls[0].headers["idempotency-key"] == "checkout-INV/2026/00001-ABC"
    responses["POST /v1/checkout/sessions"] = (400, {"error": {"message": "Invalid currency"}})
    with pytest.raises(ProviderError, match="Invalid currency"):
        adapter.create_checkout(_tx(), "u", "c")
    with pytest.raises(ProviderError):
        StripeAdapter(FakeProvider({})).create_checkout(_tx(), "u", "c")  # no secret key


def _event(event_type: str, obj: dict[str, Any], event_id: str = "evt_1") -> bytes:
    return json.dumps({"id": event_id, "type": event_type, "data": {"object": obj}}).encode()


def test_webhook_events(stripe_calls):  # type: ignore[no-untyped-def]
    _calls, responses = stripe_calls
    adapter = StripeAdapter(FakeProvider({"secret_key": "sk_test_x", "webhook_secret": SECRET}))
    session = {
        "id": "cs_1",
        "client_reference_id": "REF-1",
        "payment_status": "paid",
        "amount_total": 12050,
        "currency": "eur",
        "payment_intent": "pi_1",
        "customer": "cus_1",
    }
    responses["GET /v1/payment_intents/pi_1"] = (
        200,
        {"id": "pi_1", "payment_method": {"id": "pm_1", "card": {"brand": "visa", "last4": "4242"}}},
    )
    body = _event("checkout.session.completed", session)
    (event,) = adapter.handle_webhook({"Stripe-Signature": sign(body, SECRET)}, body)
    assert (event.state, event.reference, event.amount, event.currency) == ("done", "REF-1", 120.5, "EUR")
    assert event.provider_reference == "pi_1" and event.token and event.token.display == "Visa •••• 4242"
    assert event.token.provider_ref == "cus_1:pm_1"
    failed = _event(
        "payment_intent.payment_failed",
        {"id": "pi_2", "metadata": {"reference": "REF-2"}, "amount": 500, "currency": "usd",
         "last_payment_error": {"message": "Your card has insufficient funds."}},
        "evt_2",
    )  # fmt: skip
    (event,) = adapter.handle_webhook({"stripe-signature": sign(failed, SECRET)}, failed)
    assert event.state == "error" and event.message == "Your card has insufficient funds."
    expired = _event("checkout.session.expired", {"id": "cs_3", "client_reference_id": "REF-3"}, "evt_3")
    assert adapter.handle_webhook({"Stripe-Signature": sign(expired, SECRET)}, expired)[0].state == "cancel"
    other = _event("customer.created", {"id": "cus_9"}, "evt_4")
    assert adapter.handle_webhook({"Stripe-Signature": sign(other, SECRET)}, other)[0].state == "ignored"
    refunded = _event("charge.refunded", {"id": "ch_1", "payment_intent": "pi_1"}, "evt_5")
    assert adapter.handle_webhook({"Stripe-Signature": sign(refunded, SECRET)}, refunded)[0].state == "refunded"
    with pytest.raises(SignatureError):
        adapter.handle_webhook({"Stripe-Signature": sign(body, "wrong")}, body)
    with pytest.raises(ProviderError):
        adapter.handle_webhook({"Stripe-Signature": sign(b"not json", SECRET)}, b"not json")


def test_fetch_status_charge_and_refund(stripe_calls):  # type: ignore[no-untyped-def]
    calls, responses = stripe_calls
    adapter = StripeAdapter(FakeProvider({"secret_key": "sk_test_x"}))
    responses["GET /v1/checkout/sessions/cs_9"] = (
        200,
        {"id": "cs_9", "payment_status": "unpaid", "status": "open", "client_reference_id": "R"},
    )
    assert adapter.fetch_status(_tx(provider_reference="cs_9"))[0].state == "pending"
    assert adapter.fetch_status(_tx(provider_reference="pi_1")) == []
    token = SimpleNamespace(_provider_ref=lambda: "cus_1:pm_1")
    responses["POST /v1/payment_intents"] = (
        200,
        {"id": "pi_5", "status": "succeeded", "amount_received": 999, "amount": 999},
    )
    event = adapter.charge_token(token, 9.99, "USD", "SUB-1")
    assert event.state == "done" and event.amount == 9.99
    assert parse_qs(calls[-1].content.decode())["off_session"] == ["true"]
    responses["POST /v1/payment_intents"] = (
        402,
        {"error": {"type": "card_error", "message": "Your card was declined.", "payment_intent": {"id": "pi_6"}}},
    )
    declined = adapter.charge_token(token, 9.99, "USD", "SUB-2")
    assert declined.state == "error" and declined.message == "Your card was declined."
    with pytest.raises(ProviderError):
        adapter.charge_token(SimpleNamespace(_provider_ref=lambda: None), 1, "USD", "X")
    responses["POST /v1/refunds"] = (200, {"id": "re_1", "status": "succeeded", "amount": 5000})
    assert adapter.refund(_tx(), 50.0).state == "refunded"
    with pytest.raises(ProviderError):
        adapter.refund(_tx(provider_reference=None), 1.0)


def test_network_failure(monkeypatch):  # type: ignore[no-untyped-def]
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    monkeypatch.setattr(stripe_mod, "transport_factory", lambda: httpx.MockTransport(boom))
    with pytest.raises(ProviderError, match="could not be reached"):
        StripeAdapter(FakeProvider({"secret_key": "sk"})).create_checkout(_tx(), "u", "c")
    assert time.time() > 0
