"""Payment provider adapters, by provider code."""

from __future__ import annotations

from typing import Any

from modules.payment.providers.base import (
    CheckoutResult,
    ProviderAdapter,
    ProviderError,
    ProviderEvent,
    SignatureError,
    TokenInfo,
)
from modules.payment.providers.stripe import StripeAdapter
from modules.payment.providers.testing import TestAdapter

ADAPTERS: dict[str, type[ProviderAdapter]] = {"stripe": StripeAdapter, "test": TestAdapter}

__all__ = [
    "ADAPTERS",
    "CheckoutResult",
    "ProviderAdapter",
    "ProviderError",
    "ProviderEvent",
    "SignatureError",
    "TokenInfo",
    "adapter_for",
    "register_adapter",
]


def register_adapter(code: str, adapter: type[ProviderAdapter]) -> None:
    """Modules can add providers (``payment.provider.code`` gets the selection value through ``_extend``)."""
    ADAPTERS[code] = adapter


def adapter_for(provider: Any) -> ProviderAdapter:
    from tessera.config import get_settings

    if provider.code == "test" and get_settings().is_production:
        # the test provider settles every checkout without money and its default webhook secret is public:
        # in production it would let any customer mark an invoice paid (U-9 SEC-54)
        raise ProviderError("The test payment provider is not available in production")
    cls = ADAPTERS.get(provider.code)
    if cls is None:
        raise ProviderError(f"No adapter for provider code {provider.code!r}")
    return cls(provider)
