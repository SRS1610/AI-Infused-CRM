"""Mock access point: the "network" is the ``account.edi_mock_transmission`` table.

``send`` records the document in transit; ``status`` delivers it on the first poll (receivers whose address
contains ``reject`` refuse it); ``receive`` hands a company the delivered documents addressed to its Peppol
address. Two companies of the same database can therefore exchange invoices end to end (2-h).
"""

from __future__ import annotations

import secrets

from modules.account_einvoice.access_point.base import (
    AccessPointAdapter,
    AccessPointError,
    InboundDocument,
    OutboundDocument,
    TransmissionStatus,
)


class MockAccessPoint(AccessPointAdapter):
    code = "mock"

    def _table(self):  # type: ignore[no-untyped-def]
        return self.company.env["account.edi_mock_transmission"].elevated("mock access point")

    def send(self, document: OutboundDocument) -> str:
        if not document.receiver or ":" not in document.receiver:
            raise AccessPointError("The receiver has no Peppol address")
        transmission_id = f"MOCK-{secrets.token_hex(8).upper()}"
        self._table().create(
            {
                "transmission_id": transmission_id,
                "company_id": self.company.ids[0],
                "sender": document.sender,
                "receiver": document.receiver,
                "document_type": document.document_type,
                "payload": document.payload.decode("utf-8"),
            }
        )
        return transmission_id

    def status(self, transmission_id: str) -> TransmissionStatus:
        row = self._table().search([("transmission_id", "=", transmission_id)], limit=1)
        if not row:
            return TransmissionStatus(state="error", message="Unknown transmission")
        if row.state == "in_transit":
            if "reject" in row.receiver.lower():
                row.write({"state": "rejected", "message": "Receiver unknown on the network (mock)"})
            else:
                row.write({"state": "delivered", "message": "Delivered to the receiver's access point (mock)"})
        state = {"delivered": "delivered", "rejected": "rejected"}.get(row.state, "sent")
        return TransmissionStatus(state=state, message=row.message)

    def receive(self) -> list[InboundDocument]:
        from modules.account_einvoice.ubl import endpoint_of

        endpoint = endpoint_of(self.company.elevated())
        if endpoint is None:
            return []
        address = f"{endpoint[0]}:{endpoint[1]}"
        rows = self._table().search(
            [("receiver", "=", address), ("state", "=", "delivered"), ("picked_up", "=", False)]
        )
        out = [
            InboundDocument(
                transmission_id=r.transmission_id,
                sender=r.sender,
                receiver=r.receiver,
                payload=r.payload.encode("utf-8"),
            )
            for r in rows
        ]
        rows.write({"picked_up": True})
        return out
