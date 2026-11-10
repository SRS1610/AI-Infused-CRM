"""E-invoicing records (ERD_PHASE_2 §5): ``account.edi_document`` per sent or received document, Peppol
addresses on partners and companies, UBL tax categories on taxes, and the mock access point's network."""

from __future__ import annotations

import re
from typing import Any

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

TAX_CATEGORIES = [
    ("S", "S — Standard rate"),
    ("Z", "Z — Zero rated goods"),
    ("E", "E — Exempt from tax"),
    ("AE", "AE — VAT reverse charge"),
    ("K", "K — Intra-community supply"),
    ("G", "G — Export outside the EU"),
    ("O", "O — Outside the scope of VAT"),
    ("L", "L — Canary Islands tax (IGIC)"),
    ("M", "M — Ceuta and Melilla tax (IPSI)"),
]
EDI_STATES = [
    ("to_send", "To send"),
    ("sent", "Sent"),
    ("delivered", "Delivered"),
    ("rejected", "Rejected"),
    ("error", "Error"),
    ("received", "Received"),
]
EAS_HELP = (
    "Peppol electronic address scheme (EAS): 0088 GLN, 0060 DUNS, 0009 FR SIRET, 0208 BE enterprise number, "
    "0192 NO organisation number, 0007 SE organisation number, 0106 NL KvK, 0204 DE Leitweg-ID, "
    "9957 FR VAT, 9925 BE VAT, 9930 DE VAT, 9944 NL VAT, 9914 AT VAT, 9920 ES VAT, 0211 IT VAT …"
)


def _check_eas(record: Model) -> None:
    if record.peppol_eas and not re.fullmatch(r"\d{4}", record.peppol_eas):
        raise ValidationError(_("The Peppol scheme is a 4-digit code (e.g. 0088)"), field="peppol_eas")


class PartnerPeppol(Model):
    _extend = "contacts.partner"

    peppol_eas = fields.Char("Peppol scheme", size=4, help=EAS_HELP)
    peppol_endpoint = fields.Char("Peppol address", help="Electronic address on the Peppol network (with the scheme)")

    @api.constrains("peppol_eas")
    def _check_peppol(self) -> None:
        for partner in self:
            _check_eas(partner)


class CompanyPeppol(Model):
    _extend = "core.company"

    peppol_eas = fields.Char("Peppol scheme", size=4, help=EAS_HELP)
    peppol_endpoint = fields.Char("Peppol address")
    peppol_access_point = fields.Selection(
        [("mock", "Mock access point (testing)")], "Access point", default="mock", help="Service that sends documents"
    )
    company_registry = fields.Char("Company registry number", help="Legal registration identifier (e.g. SIREN)")

    @api.constrains("peppol_eas")
    def _check_peppol(self) -> None:
        for company in self:
            _check_eas(company)


class TaxEdi(Model):
    _extend = "account.tax"

    edi_tax_category = fields.Selection(
        TAX_CATEGORIES, "E-invoice tax category", help="UNCL5305 code; empty = derived from the rate and name"
    )
    edi_exemption_reason_code = fields.Char(
        "Exemption reason code", help="VATEX code, e.g. VATEX-EU-IC (intra-EU), VATEX-EU-G (export), VATEX-EU-AE"
    )
    edi_exemption_reason = fields.Char("Exemption reason", translate=True)


class MoveEdi(Model):
    _extend = "account.move"

    edi_document_ids = fields.One2many("account.edi_document", "move_id", "E-invoices")
    edi_state = fields.Selection(EDI_STATES, "E-invoice", compute="_compute_edi_state")

    @api.depends("edi_document_ids.state")
    def _compute_edi_state(self) -> None:
        for move in self:
            docs = move.edi_document_ids.sorted(lambda d: d.ids[0])
            move.edi_state = docs[-1].state if docs else None

    @api.expose
    def action_send_peppol(self) -> dict[str, Any]:
        """Generate, validate and send the UBL invoice through the company's access point."""
        from modules.account_einvoice.edi import send_moves

        self.check_access("write")
        docs = send_moves(self)
        return {"documents": [{"id": d.ids[0], "state": d.state, "errors": d.error_count} for d in docs]}

    @api.expose
    def action_generate_ubl(self) -> dict[str, Any]:
        """Generate and validate the UBL document without sending it (attached to the invoice)."""
        from modules.account_einvoice.edi import generate_document

        self.ensure_one()
        self.check_access("read")
        doc = generate_document(self)
        return {
            "id": doc.ids[0],
            "state": doc.state,
            "attachment_id": doc.attachment_id.ids[0] if doc.attachment_id else None,
        }


class EdiDocument(Model):
    _name = "account.edi_document"
    _description = "E-invoice"
    _order = "id desc"

    move_id = fields.Many2one("account.move", "Invoice", ondelete="cascade", index=True)
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", index=True)
    direction = fields.Selection([("out", "Sent"), ("in", "Received")], default="out", required=True)
    format = fields.Selection(
        [("ubl_21", "UBL 2.1"), ("peppol_bis3", "Peppol BIS Billing 3.0")], default="peppol_bis3", required=True
    )
    state = fields.Selection(EDI_STATES, default="to_send", required=True, index=True)
    attachment_id = fields.Many2one("core.attachment", "Document", ondelete="set_null")
    transmission_id = fields.Char(index=True)
    access_point = fields.Char()
    sender = fields.Char("Sender address")
    receiver = fields.Char("Receiver address")
    errors = fields.Json(help="[{rule_id, flag, location, message, source}] from validation or the access point")
    error_count = fields.Integer(compute="_compute_error_count")
    message = fields.Text("Status message")
    last_poll = fields.Datetime(readonly=True)

    @api.depends("errors")
    def _compute_error_count(self) -> None:
        for doc in self:
            doc.error_count = len([e for e in (doc.errors or []) if e.get("flag") in ("fatal", "error")])

    @api.depends("move_id", "direction", "state")
    def _compute_display_name(self) -> None:
        for doc in self:
            name = doc.move_id.name if doc.move_id else (doc.transmission_id or "")
            doc.display_name = f"{name} ({doc.format})"

    @api.expose
    def action_retry(self) -> str:
        from modules.account_einvoice.edi import send_moves

        self.ensure_one()
        if self.direction != "out" or self.state not in ("error", "rejected", "to_send") or not self.move_id:
            return self.state
        send_moves(self.move_id)
        return self.move_id.edi_document_ids.sorted(lambda d: d.ids[0])[-1].state


class MockTransmission(Model):
    """The mock access point's network: what was sent to whom, and its delivery status."""

    _name = "account.edi_mock_transmission"
    _description = "Mock Peppol transmission"
    _order = "id desc"

    transmission_id = fields.Char(required=True, index=True)
    company_id = fields.Many2one("core.company", "Sender company", ondelete="cascade", index=True)
    sender = fields.Char(required=True)
    receiver = fields.Char(required=True, index=True)
    document_type = fields.Char()
    payload = fields.Text(required=True)
    state = fields.Selection(
        [("in_transit", "In transit"), ("delivered", "Delivered"), ("rejected", "Rejected")],
        default="in_transit",
        required=True,
    )
    message = fields.Char()
    picked_up = fields.Boolean("Received by the recipient", index=True)
