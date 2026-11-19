"""Stripe over its REST API (``httpx``, no SDK): Checkout Sessions, webhooks, off-session charges, refunds.

Webhooks are authenticated with the endpoint's signing secret: the ``Stripe-Signature`` header carries
``t=<timestamp>,v1=<signature>[,v1=…]`` where each signature is ``HMAC-SHA256(secret, "<t>.<raw body>")``;
the timestamp must be within five minutes and signatures are compared in constant time.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

import httpx

from modules.payment.providers.base import (
    CheckoutResult,
    ProviderAdapter,
    ProviderError,
    ProviderEvent,
    SignatureError,
    TokenInfo,
    from_minor,
    to_minor,
)

API_BASE = "https://api.stripe.com"
TOLERANCE_SECONDS = 300
# tests replace this to route requests to an ``httpx.MockTransport``
transport_factory: Callable[[], httpx.BaseTransport | None] = lambda: None  # noqa: E731


def verify_signature(header: str | None, body: bytes, secret: str, *, now: float | None = None) -> int:
    """Check a ``Stripe-Signature`` header; returns its timestamp."""
    if not header or not secret:
        raise SignatureError("missing signature")
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _sep, value = part.strip().partition("=")
        if key == "t" and value.isdigit():
            timestamp = int(value)
        elif key == "v1" and value:
            signatures.append(value)
    if timestamp is None or not signatures:
        raise SignatureError("malformed signature header")
    current = time.time() if now is None else now
    if abs(current - timestamp) > TOLERANCE_SECONDS:
        raise SignatureError("signature timestamp outside the tolerance")
    signed = f"{timestamp}.".encode() + body
    expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise SignatureError("signature mismatch")
    return timestamp


def sign(body: bytes, secret: str, timestamp: int | None = None) -> str:
    """A valid ``Stripe-Signature`` header (tests and local tooling)."""
    ts = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode("utf-8"), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: ``a[b][0][c]=v``."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if isinstance(value, dict):
            out.extend(_flatten(value, name))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.extend(_flatten(item, f"{name}[{i}]"))
                else:
                    out.append((f"{name}[{i}]", str(item)))
        elif value is not None:
            out.append((name, "true" if value is True else "false" if value is False else str(value)))
    return out


class StripeAdapter(ProviderAdapter):
    code = "stripe"

    def _client(self) -> httpx.Client:
        secret = self.provider._secret("secret_key")
        if not secret:
            raise ProviderError("The Stripe secret key is not set")
        return httpx.Client(
            base_url=API_BASE,
            auth=(secret, ""),
            timeout=20.0,
            transport=transport_factory(),
            headers={"Stripe-Version": "2024-06-20"},
        )

    def _request(
        self, method: str, path: str, data: dict[str, Any] | None = None, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        content = None
        if data:
            content = urlencode(_flatten(data)).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        try:
            with self._client() as client:
                response = client.request(method, path, content=content, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"Stripe could not be reached: {exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ProviderError(f"Unexpected answer from Stripe (HTTP {response.status_code})") from exc
        if response.status_code >= 400:
            error = payload.get("error", {}) if isinstance(payload, dict) else {}
            message = error.get("message") or f"HTTP {response.status_code}"
            if error.get("type") == "card_error" or response.status_code == 402:
                return {"_card_error": message, "_error": error}
            raise ProviderError(f"Stripe: {message}")
        return payload  # type: ignore[no-any-return]

    # -- checkout ----------------------------------------------------------------------------------

    def create_checkout(self, tx: Any, return_url: str, cancel_url: str) -> CheckoutResult:
        currency = tx.currency_id.name.lower()
        data: dict[str, Any] = {
            "mode": "payment",
            "success_url": return_url,
            "cancel_url": cancel_url,
            "client_reference_id": tx.reference,
            "metadata": {"reference": tx.reference},
            "payment_intent_data": {"metadata": {"reference": tx.reference}, "description": tx.reference},
            "line_items": [
                {
                    "quantity": 1,
                    "price_data": {
                        "currency": currency,
                        "unit_amount": to_minor(tx.amount, currency),
                        "product_data": {"name": tx.reference},
                    },
                }
            ],
        }
        email = tx.partner_id.email if tx.partner_id else None
        if email:
            data["customer_email"] = email
        if tx.tokenize:
            data["customer_creation"] = "always"
            data["payment_intent_data"]["setup_future_usage"] = "off_session"
        session = self._request("POST", "/v1/checkout/sessions", data, idempotency_key=f"checkout-{tx.reference}")
        if "_card_error" in session or not session.get("url"):
            raise ProviderError(session.get("_card_error") or "Stripe did not return a checkout URL")
        return CheckoutResult(redirect_url=session["url"], provider_reference=session.get("id"))

    def fetch_status(self, tx: Any) -> list[ProviderEvent]:
        if not tx.provider_reference or not str(tx.provider_reference).startswith("cs_"):
            return []
        session = self._request("GET", f"/v1/checkout/sessions/{tx.provider_reference}")
        event = self._session_event(
            f"return:{session.get('id')}:{session.get('payment_status')}", "checkout.session.return", session
        )
        return [event] if event.state != "ignored" else []

    # -- webhooks ----------------------------------------------------------------------------------

    def handle_webhook(self, headers: dict[str, str], body: bytes) -> list[ProviderEvent]:
        lowered = {k.lower(): v for k, v in headers.items()}
        verify_signature(lowered.get("stripe-signature"), body, self.provider._secret("webhook_secret") or "")
        try:
            event = json.loads(body)
        except ValueError as exc:
            raise ProviderError("The notification is not JSON") from exc
        event_id, event_type = event.get("id"), event.get("type", "")
        obj = (event.get("data") or {}).get("object") or {}
        if not event_id:
            raise ProviderError("The notification has no id")
        if event_type.startswith("checkout.session."):
            return [self._session_event(event_id, event_type, obj)]
        if event_type.startswith("payment_intent."):
            return [self._intent_event(event_id, event_type, obj)]
        if event_type in ("charge.refunded", "refund.updated", "refund.created"):
            return [
                ProviderEvent(
                    event_id=event_id,
                    event_type=event_type,
                    state="refunded",
                    reference=(obj.get("metadata") or {}).get("reference"),
                    provider_reference=obj.get("payment_intent"),
                    raw=event,
                )
            ]
        return [ProviderEvent(event_id=event_id, event_type=event_type, state="ignored", raw=event)]

    def _session_event(self, event_id: str, event_type: str, session: dict[str, Any]) -> ProviderEvent:
        status = session.get("payment_status")
        if event_type.endswith("expired"):
            state = "cancel"
        elif event_type.endswith("async_payment_failed"):
            state = "error"
        elif status in ("paid", "no_payment_required"):
            state = "done"
        elif session.get("status") == "expired":
            state = "cancel"
        elif event_type.endswith("completed") or event_type.endswith("return"):
            state = "pending"
        else:
            state = "ignored"
        currency = session.get("currency")
        token = None
        if state == "done" and session.get("customer") and session.get("payment_intent"):
            token = self._token_of(session["payment_intent"], session["customer"])
        return ProviderEvent(
            event_id=event_id,
            event_type=event_type,
            state=state,
            reference=session.get("client_reference_id") or (session.get("metadata") or {}).get("reference"),
            provider_reference=session.get("payment_intent") or session.get("id"),
            amount=from_minor(session.get("amount_total"), currency),
            currency=currency.upper() if currency else None,
            message="The checkout session expired" if state == "cancel" else None,
            token=token,
            raw={"id": event_id, "type": event_type, "object": session.get("id")},
        )

    def _token_of(self, intent_id: str, customer: str) -> TokenInfo | None:
        try:
            intent = self._request("GET", f"/v1/payment_intents/{intent_id}?expand[]=payment_method")
        except ProviderError:
            return None
        method = intent.get("payment_method") or {}
        if not isinstance(method, dict) or not method.get("id"):
            return None
        card = method.get("card") or {}
        display = f"{(card.get('brand') or 'card').title()} •••• {card.get('last4') or '????'}"
        return TokenInfo(provider_ref=f"{customer}:{method['id']}", display=display)

    def _intent_event(self, event_id: str, event_type: str, intent: dict[str, Any]) -> ProviderEvent:
        mapping = {
            "payment_intent.succeeded": "done",
            "payment_intent.payment_failed": "error",
            "payment_intent.canceled": "cancel",
            "payment_intent.amount_capturable_updated": "authorized",
            "payment_intent.processing": "pending",
        }
        currency = intent.get("currency")
        error = intent.get("last_payment_error") or {}
        return ProviderEvent(
            event_id=event_id,
            event_type=event_type,
            state=mapping.get(event_type, "ignored"),
            reference=(intent.get("metadata") or {}).get("reference"),
            provider_reference=intent.get("id"),
            amount=from_minor(intent.get("amount_received") or intent.get("amount"), currency),
            currency=currency.upper() if currency else None,
            message=error.get("message") or intent.get("cancellation_reason"),
            raw={"id": event_id, "type": event_type, "object": intent.get("id")},
        )

    # -- tokens and refunds ------------------------------------------------------------------------

    def charge_token(self, token: Any, amount: float, currency: str, reference: str) -> ProviderEvent:
        customer, _sep, method = (token._provider_ref() or "").partition(":")
        if not customer or not method:
            raise ProviderError("The saved payment method is not usable")
        intent = self._request(
            "POST",
            "/v1/payment_intents",
            {
                "amount": to_minor(amount, currency),
                "currency": currency.lower(),
                "customer": customer,
                "payment_method": method,
                "off_session": True,
                "confirm": True,
                "description": reference,
                "metadata": {"reference": reference},
            },
            idempotency_key=f"charge-{reference}",
        )
        if "_card_error" in intent:
            error = intent.get("_error") or {}
            pi = error.get("payment_intent") or {}
            return ProviderEvent(
                event_id=f"charge:{reference}:error",
                event_type="payment_intent.payment_failed",
                state="error",
                reference=reference,
                provider_reference=pi.get("id"),
                message=intent["_card_error"],
            )
        status = intent.get("status")
        state = {"succeeded": "done", "processing": "pending", "requires_capture": "authorized"}.get(
            status or "", "error"
        )
        return ProviderEvent(
            event_id=f"charge:{intent.get('id')}:{status}",
            event_type="payment_intent.charge",
            state=state,
            reference=reference,
            provider_reference=intent.get("id"),
            amount=from_minor(intent.get("amount_received") or intent.get("amount"), currency),
            currency=currency.upper(),
            message=None if state != "error" else f"Payment requires an action from the customer ({status})",
        )

    def refund(self, tx: Any, amount: float) -> ProviderEvent:
        if not tx.provider_reference:
            raise ProviderError("The transaction has no Stripe payment to refund")
        currency = tx.currency_id.name
        refund = self._request(
            "POST",
            "/v1/refunds",
            {
                "payment_intent": tx.provider_reference,
                "amount": to_minor(amount, currency),
                "metadata": {"reference": tx.reference},
            },
            idempotency_key=f"refund-{tx.reference}-{to_minor(amount, currency)}",
        )
        if "_card_error" in refund:
            raise ProviderError(refund["_card_error"])
        state = "refunded" if refund.get("status") in ("succeeded", "pending") else "error"
        return ProviderEvent(
            event_id=f"refund:{refund.get('id')}",
            event_type="refund",
            state=state,
            reference=tx.reference,
            provider_reference=refund.get("id"),
            amount=from_minor(refund.get("amount"), currency),
            currency=currency,
            message=refund.get("failure_reason"),
        )
