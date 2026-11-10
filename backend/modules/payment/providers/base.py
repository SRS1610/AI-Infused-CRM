"""Payment provider adapter interface.

An adapter talks to one payment service for one ``payment.provider`` record. It never writes to the
database: it returns :class:`ProviderEvent` objects that ``modules.payment.service.process_event`` applies
(idempotently, keyed on ``(provider, event_id)``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# currencies without minor units (amounts are sent as whole numbers)
ZERO_DECIMAL = {
    "BIF",
    "CLP",
    "DJF",
    "GNF",
    "JPY",
    "KMF",
    "KRW",
    "MGA",
    "PYG",
    "RWF",
    "UGX",
    "VND",
    "VUV",
    "XAF",
    "XOF",
    "XPF",
}
EVENT_STATES = ("pending", "authorized", "done", "cancel", "error", "refunded", "ignored")


class ProviderError(Exception):
    """The provider refused a request or answered something unusable (message is safe to show)."""


class SignatureError(ProviderError):
    """A webhook whose signature is missing, wrong or too old."""


@dataclass
class TokenInfo:
    provider_ref: str
    display: str


@dataclass
class ProviderEvent:
    """What happened to a transaction, as reported by the provider."""

    event_id: str
    event_type: str
    state: str  # one of EVENT_STATES
    reference: str | None = None
    provider_reference: str | None = None
    amount: float | None = None
    currency: str | None = None
    message: str | None = None
    token: TokenInfo | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckoutResult:
    redirect_url: str
    provider_reference: str | None = None
    events: list[ProviderEvent] = field(default_factory=list)  # adapters that settle at once (test)


def to_minor(amount: float, currency: str) -> int:
    factor = 1 if currency.upper() in ZERO_DECIMAL else 100
    return round(float(amount) * factor)


def from_minor(amount: int | None, currency: str | None) -> float | None:
    if amount is None:
        return None
    factor = 1 if (currency or "").upper() in ZERO_DECIMAL else 100
    return float(amount) / factor


class ProviderAdapter(ABC):
    """One payment service. ``provider`` is the (elevated) ``payment.provider`` record."""

    code: str = ""

    def __init__(self, provider: Any) -> None:
        self.provider = provider

    @abstractmethod
    def create_checkout(self, tx: Any, return_url: str, cancel_url: str) -> CheckoutResult:
        """Start a hosted payment for ``tx``; returns the URL to send the customer to."""

    @abstractmethod
    def handle_webhook(self, headers: dict[str, str], body: bytes) -> list[ProviderEvent]:
        """Verify and parse a notification (raises :class:`SignatureError` when it is not authentic)."""

    @abstractmethod
    def charge_token(self, token: Any, amount: float, currency: str, reference: str) -> ProviderEvent:
        """Charge a saved payment method without the customer (recurring billing)."""

    @abstractmethod
    def refund(self, tx: Any, amount: float) -> ProviderEvent:
        """Refund (part of) a settled transaction."""

    def fetch_status(self, tx: Any) -> list[ProviderEvent]:
        """Ask the provider for the transaction's current state (when the customer returns). Optional."""
        return []
