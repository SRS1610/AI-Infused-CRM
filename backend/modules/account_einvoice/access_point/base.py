"""Peppol access-point adapter interface.

An access point transmits documents on the Peppol network for a company. Adapters return plain data; the
EDI service (``modules.account_einvoice.edi``) updates ``account.edi_document`` records.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class AccessPointError(Exception):
    """The access point refused the request (message is safe to show)."""


@dataclass
class OutboundDocument:
    sender: str  # "<scheme>:<id>"
    receiver: str
    document_type: str  # Invoice / CreditNote
    payload: bytes
    reference: str


@dataclass
class TransmissionStatus:
    state: str  # sent / delivered / rejected / error
    message: str | None = None
    errors: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class InboundDocument:
    transmission_id: str
    sender: str
    receiver: str
    payload: bytes


class AccessPointAdapter(ABC):
    code = ""

    def __init__(self, company: Any) -> None:
        self.company = company

    @abstractmethod
    def send(self, document: OutboundDocument) -> str:
        """Hand the document to the network; returns the transmission id."""

    @abstractmethod
    def status(self, transmission_id: str) -> TransmissionStatus:
        """Where the transmission is (delivered to the receiver's access point, rejected …)."""

    @abstractmethod
    def receive(self) -> list[InboundDocument]:
        """Documents addressed to the company that have not been collected yet."""
