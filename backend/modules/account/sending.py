"""Emailing invoices with their PDF and a link to view them online."""

from __future__ import annotations

import html
from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model


def send_invoice(moves: Model, email_to: str | None = None) -> dict[str, Any]:
    from modules.chatter.thread import post_message, reply_address
    from tessera.config import get_settings

    moves.ensure_one()
    move = moves
    if move.state != "posted" or move.move_type not in ("out_invoice", "out_refund"):
        raise UserError(_("Only posted customer invoices and credit notes can be sent"))
    recipient = email_to or move.partner_id.email or move.partner_id.commercial_partner_id.email
    if not recipient:
        raise UserError(_("The customer has no email address"))
    attachment_ids: list[int] = []
    template = move.env.ref("account.report_invoice", raise_if_not_found=False)
    if template is not None:
        content, _mimetype, filename = template.render(move.ids, "pdf")
        att = (
            move.env["core.attachment"]
            .elevated()
            .create(
                {
                    "name": filename,
                    "mimetype": "application/pdf",
                    "model": move._name,
                    "res_id": move.ids[0],
                    "raw": content,
                }
            )
        )
        attachment_ids = att.ids
    link = f"{get_settings().public_url.rstrip('/')}{move.portal_url()}"
    kind = _("credit note") if move.move_type == "out_refund" else _("invoice")
    intro = _(
        "Please find our {kind} {number} for {amount}, due on {due}.",
        kind=kind,
        number=move.name,
        amount=move.currency_id.format(move.amount_total),
        due=move.invoice_date_due,
    )
    # names and numbers are user data: no markup reaches the e-mail (U-9 SEC-56)
    body = (
        f"<p>{html.escape(str(_('Hello {name},', name=move.partner_id.name)))}</p>"
        f"<p>{html.escape(str(intro))}</p>"
        f'<p><a href="{html.escape(link)}">{html.escape(str(_("View it online")))}</a></p>'
    )
    move.env["core.mail_queue"].queue(
        {
            "email_to": recipient,
            "subject": _("{company} {kind} {number}", company=move.company_id.name, kind=kind, number=move.name),
            "body_html": body,
            "reply_to": reply_address(move._name, move.ids[0]),
            "model": move._name,
            "res_id": move.ids[0],
            "attachment_ids": [("set", attachment_ids)],
        }
    )
    post_message(
        move.env,
        move._name,
        move.ids[0],
        f"<p>{html.escape(str(_('Sent to {recipient}.', recipient=recipient)))}</p>",
        message_type="notification",
        attachment_ids=attachment_ids,
        notify=False,
    )
    return {"portal_url": link}
