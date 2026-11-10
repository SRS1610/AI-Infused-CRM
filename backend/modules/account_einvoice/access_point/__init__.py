"""Peppol access-point adapters by code (``core.company.peppol_access_point``)."""

from __future__ import annotations

from typing import Any

from modules.account_einvoice.access_point.base import (
    AccessPointAdapter,
    AccessPointError,
    InboundDocument,
    OutboundDocument,
    TransmissionStatus,
)
from modules.account_einvoice.access_point.mock import MockAccessPoint

ADAPTERS: dict[str, type[AccessPointAdapter]] = {"mock": MockAccessPoint}

__all__ = [
    "ADAPTERS",
    "AccessPointAdapter",
    "AccessPointError",
    "InboundDocument",
    "OutboundDocument",
    "TransmissionStatus",
    "adapter_for",
]


def adapter_for(company: Any) -> AccessPointAdapter:
    code = company.elevated().peppol_access_point or "mock"
    cls = ADAPTERS.get(code)
    if cls is None:
        raise AccessPointError(f"No access point adapter {code!r}")
    return cls(company)
