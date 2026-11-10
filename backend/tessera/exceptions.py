"""Kernel exception hierarchy.

``UserError`` and its subclasses carry messages meant for end users; the API layer maps
them to HTTP statuses. Anything else is a programming or infrastructure error and is
reported as a 500 without leaking its message.
"""

from __future__ import annotations

from typing import Any


class TesseraError(Exception):
    """Base class for every kernel exception."""

    http_status = 500

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context = context

    def to_dict(self) -> dict[str, Any]:
        return {"error": type(self).__name__, "message": self.message, **self.context}


class UserError(TesseraError):
    """A message for the user (business rule violated, invalid operation)."""

    http_status = 400


class ValidationError(UserError):
    """Invalid field values (type, required, selection, constraint)."""

    http_status = 422


class AccessError(UserError):
    """The current user may not perform this operation."""

    http_status = 403


class AccessDenied(AccessError):
    """Authentication failed (wrong credentials, inactive user, missing session)."""

    http_status = 401


class MissingError(UserError):
    """A record that was expected to exist does not (deleted or never existed)."""

    http_status = 404


class ConcurrencyError(UserError):
    """Optimistic concurrency conflict (stale ETag / write_date)."""

    http_status = 409


class ConfigurationError(TesseraError):
    """Invalid module, manifest, model or field definition (developer error)."""


class RegistryError(ConfigurationError):
    """The model registry could not be built."""


class ModuleError(ConfigurationError):
    """Module install / upgrade / uninstall problem."""
