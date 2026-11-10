"""E-invoice flows: generate and validate, send through the access point, poll statuses, receive bills.

Validation findings are stored on ``account.edi_document.errors`` as structured records (rule id, flag,
location, message, source) so that the 2.6 AI feature can explain them. A document with a *fatal* finding is
not sent.
"""

from __future__ import annotations

from typing import Any

from modules.account_einvoice.access_point import AccessPointError, OutboundDocument, adapter_for
from modules.account_einvoice.ubl import UblBuilder, endpoint_of
from modules.account_einvoice.validator import Finding, fatal, validate
from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.jobs import job
from tessera.logging import get_logger
from tessera.orm import Model

log = get_logger(__name__)


def _address(record: Model) -> str | None:
    endpoint = endpoint_of(record.elevated())
    return f"{endpoint[0]}:{endpoint[1]}" if endpoint else None


def _document(move: Model) -> Model:
    docs = move.env["account.edi_document"].elevated()
    existing = docs.search([("move_id", "=", move.ids[0]), ("direction", "=", "out")], order="id desc", limit=1)
    if existing and existing.state in ("sent", "delivered"):
        raise UserError(_("{name} has already been sent electronically", name=move.name))
    if existing:
        return existing
    return docs.create({"move_id": move.ids[0], "company_id": move.company_id.ids[0], "direction": "out"})


def generate_document(move: Model) -> Model:
    """Build the UBL document of ``move``, validate it and attach it to its e-invoice record."""
    move = move.elevated("e-invoice")
    doc = _document(move)
    try:
        builder = UblBuilder(move)
        xml = builder.build()
    except UserError as exc:
        doc.write(
            {
                "state": "error",
                "errors": [Finding("TESSERA-DATA", "fatal", "", str(exc), "Tessera").as_dict()],
                "message": str(exc),
            }
        )
        return doc
    findings = validate(xml)
    blocking = fatal(findings)
    old = doc.attachment_id
    attachment = (
        move.env["core.attachment"]
        .elevated()
        .create(
            {
                "name": f"{(move.name or 'invoice').replace('/', '_')}.xml",
                "mimetype": "application/xml",
                "model": "account.move",
                "res_id": move.ids[0],
                "raw": xml,
                "company_id": move.company_id.ids[0],
            }
        )
    )
    if old:
        old.unlink()
    doc.write(
        {
            "attachment_id": attachment.ids[0],
            "errors": [f.as_dict() for f in findings],
            "state": "error" if blocking else "to_send",
            "sender": _address(move.company_id),
            "receiver": _address(builder.partner),
            "message": _("{count} validation errors", count=len(blocking)) if blocking else None,
        }
    )
    return doc


def send_moves(moves: Model) -> Model:
    """Generate, validate and send each posted customer invoice / credit note; returns their documents."""
    out = moves.env["account.edi_document"].elevated()
    for move in moves:
        doc = generate_document(move)
        out |= doc
        if doc.state != "to_send":
            continue
        try:
            adapter = adapter_for(move.company_id)
            transmission = adapter.send(
                OutboundDocument(
                    sender=doc.sender or "",
                    receiver=doc.receiver or "",
                    document_type="Invoice" if move.move_type == "out_invoice" else "CreditNote",
                    payload=bytes(doc.attachment_id.elevated().raw),
                    reference=move.name,
                )
            )
        except AccessPointError as exc:
            error = Finding("ACCESS-POINT", "fatal", "", str(exc), "access point").as_dict()
            doc.write({"state": "error", "errors": [*(doc.errors or []), error], "message": str(exc)})
            continue
        doc.write({"state": "sent", "transmission_id": transmission, "access_point": adapter.code, "message": None})
    return out


def poll(company: Model) -> dict[str, Any]:
    """Update the statuses of sent documents and collect received documents as draft bills."""
    from modules.account_einvoice.ubl_import import create_bill

    env = company.env
    company = company.elevated()
    adapter = adapter_for(company)
    docs = (
        env["account.edi_document"]
        .elevated()
        .search([("company_id", "=", company.ids[0]), ("direction", "=", "out"), ("state", "=", "sent")])
    )
    updated = 0
    for doc in docs:
        status = adapter.status(doc.transmission_id)
        vals: dict[str, Any] = {"last_poll": env.now(), "message": status.message}
        if status.state in ("delivered", "rejected", "error") and status.state != doc.state:
            vals["state"] = status.state
            updated += 1
            if status.errors:
                vals["errors"] = [*(doc.errors or []), *status.errors]
        doc.write(vals)
    received = 0
    for inbound in adapter.receive():
        try:
            with env.cr.savepoint():
                bill = create_bill(company, inbound.payload, filename=f"{inbound.transmission_id}.xml")
        except UserError as exc:
            env["account.edi_document"].elevated().create(
                {
                    "company_id": company.ids[0],
                    "direction": "in",
                    "state": "error",
                    "transmission_id": inbound.transmission_id,
                    "sender": inbound.sender,
                    "receiver": inbound.receiver,
                    "message": str(exc),
                    "errors": [Finding("IMPORT", "fatal", "", str(exc), "Tessera").as_dict()],
                }
            )
            continue
        env["account.edi_document"].elevated().create(
            {
                "move_id": bill.ids[0],
                "company_id": company.ids[0],
                "direction": "in",
                "state": "received",
                "transmission_id": inbound.transmission_id,
                "sender": inbound.sender,
                "receiver": inbound.receiver,
            }
        )
        received += 1
    return {"updated": updated, "received": received}


@job("account_einvoice.poll", queue="default", max_attempts=1, as_system=True)
def poll_job(env: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        try:
            result[company.name] = poll(company)
        except AccessPointError as exc:
            log.warning("peppol poll failed", company=company.name, error=str(exc))
    return result
