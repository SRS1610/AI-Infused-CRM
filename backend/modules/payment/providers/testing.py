"""The test provider: payments settle immediately, with no external service (demo, end-to-end tests).

Checkout returns the customer to the return URL with the transaction already *done*. Saved methods whose
reference contains ``fail`` are declined, so failure handling can be tried. Its webhooks are JSON bodies
signed like Stripe's (``Test-Signature: t=…,v1=…`` with the provider's webhook secret).
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from modules.payment.providers.base import (
    CheckoutResult,
    ProviderAdapter,
    ProviderError,
    ProviderEvent,
    TokenInfo,
)
from modules.payment.providers.stripe import verify_signature

DEFAULT_SECRET = "test-provider-secret"  # noqa: S105 - the test provider is not a real service


class TestAdapter(ProviderAdapter):
    code = "test"
    __test__ = False  # not a pytest test class

    def _secret(self) -> str:
        return self.provider._secret("webhook_secret") or DEFAULT_SECRET

    def create_checkout(self, tx: Any, return_url: str, cancel_url: str) -> CheckoutResult:
        provider_ref = f"test_{secrets.token_hex(6)}"
        token = (
            TokenInfo(provider_ref=f"test-customer:{provider_ref}", display="Test card •••• 4242")
            if tx.tokenize
            else None
        )
        event = ProviderEvent(
            event_id=f"checkout:{tx.reference}",
            event_type="test.checkout",
            state="done",
            reference=tx.reference,
            provider_reference=provider_ref,
            amount=float(tx.amount),
            currency=tx.currency_id.name,
            token=token,
        )
        return CheckoutResult(redirect_url=return_url, provider_reference=provider_ref, events=[event])

    def handle_webhook(self, headers: dict[str, str], body: bytes) -> list[ProviderEvent]:
        lowered = {k.lower(): v for k, v in headers.items()}
        verify_signature(lowered.get("test-signature"), body, self._secret())
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise ProviderError("The notification is not JSON") from exc
        if not data.get("id"):
            raise ProviderError("The notification has no id")
        return [
            ProviderEvent(
                event_id=str(data["id"]),
                event_type=str(data.get("type") or "test.event"),
                state=str(data.get("state") or "ignored"),
                reference=data.get("reference"),
                provider_reference=data.get("provider_reference"),
                amount=float(data["amount"]) if data.get("amount") is not None else None,
                currency=data.get("currency"),
                message=data.get("message"),
            )
        ]

    def charge_token(self, token: Any, amount: float, currency: str, reference: str) -> ProviderEvent:
        declined = "fail" in (token._provider_ref() or "")
        return ProviderEvent(
            event_id=f"charge:{reference}",
            event_type="test.charge",
            state="error" if declined else "done",
            reference=reference,
            provider_reference=f"test_{secrets.token_hex(6)}",
            amount=float(amount),
            currency=currency,
            message="Your card was declined (test)" if declined else None,
        )

    def refund(self, tx: Any, amount: float) -> ProviderEvent:
        return ProviderEvent(
            event_id=f"refund:{tx.reference}:{amount}",
            event_type="test.refund",
            state="refunded",
            reference=tx.reference,
            provider_reference=f"test_refund_{secrets.token_hex(4)}",
            amount=float(amount),
            currency=tx.currency_id.name,
        )
