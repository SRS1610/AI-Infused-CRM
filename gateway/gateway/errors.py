"""Errors the gateway returns, each with a stable code the runtime maps to a manual path."""

from __future__ import annotations

from typing import Any


class GatewayError(Exception):
    code = "gateway_error"
    status = 500

    def __init__(self, message: str = "", **details: Any) -> None:
        super().__init__(message or self.code)
        self.message = message or self.code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


class Unauthorized(GatewayError):
    code, status = "unauthorized", 401


class FeatureDisabled(GatewayError):
    code, status = "feature_disabled", 404


class BudgetExceeded(GatewayError):
    code, status = "budget_exceeded", 429


class QuotaExceeded(BudgetExceeded):
    code = "quota_exceeded"


class InvalidOutput(GatewayError):
    code, status = "invalid_output", 422


class NoRoute(GatewayError):
    code, status = "no_route", 503


class PolicyRefused(GatewayError):
    """Residency or PII policy forbids every configured provider for this request."""

    code, status = "policy_refused", 403


class ProviderUnavailable(GatewayError):
    code, status = "provider_unavailable", 503


class NotSupported(GatewayError):
    code, status = "not_supported", 501


class ProviderError(Exception):
    """Raised by adapters. ``retryable`` errors (5xx, 429, timeouts, connection) count against the
    circuit breaker and trigger the fallback route; others (4xx) are returned to the caller."""

    def __init__(self, message: str, *, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status
