"""AI errors. Each maps to a manual path in the UI: the AI affordance shows why it is unavailable and
the ordinary workflow carries on."""

from __future__ import annotations

from tessera.exceptions import UserError


class AIError(UserError):
    code = "ai_error"
    http_status = 500

    def __init__(self, message: str = "", **context: object) -> None:
        super().__init__(message, **{"code": self.code, **context})


class AIUnavailable(AIError):
    """The gateway or every provider is down."""

    code, http_status = "ai_unavailable", 503


class FeatureDisabled(AIError):
    code, http_status = "feature_disabled", 404


class BudgetExceeded(AIError):
    code, http_status = "budget_exceeded", 429


class QuotaExceeded(BudgetExceeded):
    code = "quota_exceeded"


class InvalidOutput(AIError):
    code, http_status = "invalid_output", 422


class PolicyRefused(AIError):
    code, http_status = "policy_refused", 403


class ModelRefused(AIError):
    """The model declined (safety refusal)."""

    code, http_status = "model_refused", 422


CODES: dict[str, type[AIError]] = {
    cls.code: cls
    for cls in (
        AIUnavailable,
        FeatureDisabled,
        BudgetExceeded,
        QuotaExceeded,
        InvalidOutput,
        PolicyRefused,
        ModelRefused,
    )
}


def from_code(code: str, message: str) -> AIError:
    cls = CODES.get(code, AIUnavailable)
    return cls(message or code)
