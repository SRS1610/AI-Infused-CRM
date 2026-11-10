"""Inbound email: parse, route (reply token → thread, In-Reply-To → thread, alias → new record), post.

Entry points: ``POST /api/v1/chatter/inbound`` (MTA webhook), ``tessera mail inject <file.eml>``, and the
``chatter.fetch_mail`` job that polls IMAP mailboxes (``chatter.fetch_server``).
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import re
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

from modules.chatter.thread import is_thread, parse_reply_token, post_message
from tessera.logging import get_logger

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)


def parse_email(raw: bytes | str) -> dict[str, Any]:
    data = raw.encode() if isinstance(raw, str) else raw
    msg: EmailMessage = email.message_from_bytes(data, policy=email.policy.default)  # type: ignore[assignment]
    body_part = msg.get_body(preferencelist=("html", "plain"))
    body = ""
    if body_part is not None:
        content = body_part.get_content()
        body = content if body_part.get_content_type() == "text/html" else _text_to_html(content)
    attachments = []
    for part in msg.iter_attachments():
        payload = part.get_payload(decode=True)
        if payload is not None:
            attachments.append(
                {"name": part.get_filename() or "attachment", "mimetype": part.get_content_type(), "raw": payload}
            )
    from_name, from_addr = email.utils.parseaddr(str(msg.get("From", "")))
    headers = [str(msg.get(h)) for h in ("To", "Cc", "Delivered-To") if msg.get(h)]  # strict parser: no empties
    recipients = [a for _, a in email.utils.getaddresses(headers) if a]
    references = " ".join(str(msg.get(h, "")) for h in ("In-Reply-To", "References")).split()
    bounced_id = _bounced_message_id(msg)
    return {
        "bounce_of": bounced_id,
        "message_id": str(msg.get("Message-ID", "")).strip() or email.utils.make_msgid(),
        "subject": str(msg.get("Subject", "")).strip(),
        "from": from_addr.lower(),
        "from_name": from_name,
        "to": [r.lower() for r in recipients],
        "references": references,
        "body": body,
        "attachments": attachments,
        "auto_submitted": str(msg.get("Auto-Submitted", "no")).lower() != "no" or bool(msg.get("X-Autoreply")),
    }


def _bounced_message_id(msg: EmailMessage) -> str | None:
    """Message-ID of the original email when ``msg`` is a delivery status notification (RFC 3464)."""
    if msg.get_content_type() != "multipart/report" or msg.get_param("report-type") != "delivery-status":
        return None
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("message/rfc822", "text/rfc822-headers"):
            payload = part.get_payload()
            if isinstance(payload, list) and payload:
                original = payload[0].get("Message-ID")
            else:
                text = payload if isinstance(payload, str) else part.get_content()
                match = re.search(r"^Message-ID:\s*(<[^>]+>)", str(text), flags=re.I | re.M)
                original = match.group(1) if match else None
            if original:
                return str(original).strip()
    return None


def _text_to_html(text: str) -> str:
    import html

    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p]
    return "".join(f"<p>{html.escape(p).replace(chr(10), '<br/>')}</p>" for p in paragraphs)


def route(env: Environment, msg: dict[str, Any]) -> tuple[str, int, str] | None:
    """(model, res_id, how) for the thread this email belongs to, or None. ``how`` is reply|thread|alias."""
    for addr in msg["to"]:
        local = addr.split("@", 1)[0]
        target = parse_reply_token(local)
        if target and target[0] in env.registry.models:
            return target[0], target[1], "reply"
    if msg["references"]:
        known = env["chatter.message"].elevated().search([("message_id", "in", msg["references"])], limit=1)
        if known:
            return known.model, known.res_id, "thread"
    aliases = env["core.mail_alias"].elevated()
    for addr in msg["to"]:
        local = addr.split("@", 1)[0]
        alias = aliases.search([("alias_name", "=", local), ("active", "=", True)], limit=1)
        if alias:
            return alias.model_name, 0, f"alias:{alias.ids[0]}"
    return None


def _author_for(env: Environment, address: str) -> int | None:
    if not address:
        return None
    users = env["core.user"].elevated().search([("email", "ilike", address)], limit=10)
    exact = users.filtered(lambda u: (u.email or "").lower() == address.lower())
    return exact.ids[0] if exact else None


def process_email(env: Environment, raw: bytes | str) -> dict[str, Any]:
    """Process one inbound email as the system. Returns what happened (for logs and the API)."""
    sys_env = env.elevated("inbound mail")
    msg = parse_email(raw)
    if msg["bounce_of"]:
        queued = sys_env["core.mail_queue"].search([("message_id", "=", msg["bounce_of"])], limit=1)
        if queued:
            queued.write({"state": "bounced", "error": "Delivery failed (bounce received)"})
        return {"status": "bounce", "message_id": msg["bounce_of"], "matched": bool(queued)}
    if msg["auto_submitted"]:
        return {"status": "ignored", "reason": "auto-submitted"}
    messages = sys_env["chatter.message"]
    if messages.search_count([("message_id", "=", msg["message_id"])]):
        return {"status": "duplicate", "message_id": msg["message_id"]}
    target = route(sys_env, msg)
    if target is None:
        log.info("inbound email not routed", to=msg["to"], subject=msg["subject"][:80])
        return {"status": "bounced", "reason": "no matching alias or thread"}
    model, res_id, how = target
    author = _author_for(sys_env, msg["from"])
    record: Model
    if how.startswith("alias:"):
        alias = sys_env["core.mail_alias"].browse([int(how.split(":")[1])])
        if alias.contact_policy == "partners" and not author:
            return {"status": "bounced", "reason": "sender unknown"}
        model_env = sys_env if not alias.user_id else sys_env.with_user(alias.user_id.ids[0])
        records = model_env[model]
        if alias.company_id:
            records = records.with_company(alias.company_id.ids[0])
        if not is_thread(records):
            return {"status": "bounced", "reason": f"{model} has no thread"}
        record = records.message_new(msg, dict(alias.defaults or {}))
        res_id = record.ids[0]
    else:
        record = sys_env[model].browse([res_id]).exists()
        if not record:
            return {"status": "bounced", "reason": "thread no longer exists"}
        record.message_update(msg)
    attachment_ids = []
    for att in msg["attachments"]:
        created = sys_env["core.attachment"].create(
            {"name": att["name"], "mimetype": att["mimetype"], "raw": att["raw"], "model": model, "res_id": res_id}
        )
        attachment_ids.extend(created.ids)
    posted = post_message(
        sys_env,
        model,
        res_id,
        msg["body"],
        subject=msg["subject"],
        message_type="email",
        author_id=author,
        email_from=email.utils.formataddr((msg["from_name"], msg["from"])),
        message_id=msg["message_id"],
        attachment_ids=attachment_ids,
    )
    return {
        "status": "created" if how.startswith("alias:") else "posted",
        "model": model,
        "res_id": res_id,
        "message": posted.ids[0],
    }
