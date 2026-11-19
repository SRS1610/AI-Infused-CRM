"""Outgoing mail queue, mail servers and inbound aliases."""

from __future__ import annotations

import datetime as dt
import email.utils
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from tessera.config import get_settings
from tessera.jobs import enqueue, job
from tessera.orm import Model, api, fields
from tessera.security import crypto


class MailServer(Model):
    _name = "core.mail_server"
    _description = "Outgoing mail server"
    _order = "sequence, id"
    _audit = True

    name = fields.Char(required=True)
    company_id = fields.Many2one("core.company", ondelete="cascade", help="Empty = every company")
    host = fields.Char(required=True)
    port = fields.Integer(default=587)
    security = fields.Selection(
        [("none", "None"), ("starttls", "STARTTLS"), ("ssl", "SSL/TLS")], default="starttls", required=True
    )
    username = fields.Char()
    password = fields.Char(groups="core.group_system", help="Stored encrypted")
    from_filter = fields.Char(help="Only use this server for senders on this domain")
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("password") and not crypto.is_encrypted(vals["password"]):
                vals["password"] = crypto.encrypt(vals["password"])
        return super().create(batch)

    def write(self, vals):  # type: ignore[no-untyped-def]
        vals = dict(vals)
        if vals.get("password") and not crypto.is_encrypted(vals["password"]):
            vals["password"] = crypto.encrypt(vals["password"])
        return super().write(vals)


class MailAlias(Model):
    """Inbound address routing: ``<alias>@<catchall domain>`` creates records of ``model_name``."""

    _name = "core.mail_alias"
    _description = "Mail alias"
    _order = "alias_name"
    _sql_constraints = [("alias_uniq", "UNIQUE (alias_name)", "Alias names must be unique")]

    alias_name = fields.Char(required=True, index=True)
    model_name = fields.Char("Creates records of", required=True)
    defaults = fields.Json(default=dict, help="Values for created records")
    company_id = fields.Many2one("core.company", ondelete="cascade")
    user_id = fields.Many2one("core.user", "Create as", ondelete="set_null", help="Empty = system")
    contact_policy = fields.Selection(
        [("everyone", "Everyone"), ("partners", "Known contacts"), ("followers", "Followers")], default="everyone"
    )
    active = fields.Boolean(default=True)


class MailQueue(Model):
    _name = "core.mail_queue"
    _description = "Outgoing email"
    _order = "id desc"
    _rec_name = "subject"
    _studio = False
    _indexes = [("state_next", ["state", "next_attempt_at"])]

    state = fields.Selection(
        [
            ("outgoing", "Outgoing"),
            ("sent", "Sent"),
            ("failed", "Failed"),
            ("bounced", "Bounced"),
            ("cancelled", "Cancelled"),
        ],
        default="outgoing",
        required=True,
        index=True,
    )
    email_from = fields.Char(required=True)
    email_to = fields.Char(required=True)
    email_cc = fields.Char()
    reply_to = fields.Char()
    subject = fields.Char(required=True)
    body_html = fields.Html(sanitize=False, help="Rendered by trusted templates")
    body_text = fields.Text()
    message_id = fields.Char("Message-ID", index=True)
    in_reply_to = fields.Char()
    references = fields.Text()
    headers = fields.Json(default=dict)
    attachment_ids = fields.Many2many(
        "core.attachment", relation="core_mail_queue__attachment_ids", column1="mail_id", column2="attachment_id"
    )
    model = fields.Char()
    res_id = fields.Integer()
    chatter_message_id = fields.Integer()
    company_id = fields.Many2one("core.company", ondelete="set_null", fk=False)
    attempts = fields.Integer(default=0)
    next_attempt_at = fields.Datetime()
    sent_at = fields.Datetime()
    error = fields.Text()

    @api.model
    def queue(self, vals: dict[str, Any]) -> Model:
        settings = get_settings()
        vals = dict(vals)
        vals.setdefault("email_from", settings.mail_from)
        vals.setdefault("message_id", email.utils.make_msgid(domain=_mail_domain()))
        mail = self.elevated("mail queue").create(vals)
        enqueue(
            self.env.elevated(),
            "core.send_mail",
            {"mail_id": mail.ids[0]},
            queue="mail",
            idempotency_key=f"mail:{mail.ids[0]}",
        )
        return mail

    def build_message(self) -> EmailMessage:
        self.ensure_one()
        msg = EmailMessage()
        msg["From"] = self.email_from
        msg["To"] = self.email_to
        if self.email_cc:
            msg["Cc"] = self.email_cc
        if self.reply_to:
            msg["Reply-To"] = self.reply_to
        msg["Subject"] = self.subject
        msg["Message-ID"] = self.message_id
        msg["Date"] = email.utils.format_datetime(dt.datetime.now(dt.UTC))
        if self.in_reply_to:
            msg["In-Reply-To"] = self.in_reply_to
        if self.references:
            msg["References"] = self.references
        for key, value in (self.headers or {}).items():
            msg[str(key)] = str(value)
        text = self.body_text or _html_to_text(self.body_html or "")
        msg.set_content(text)
        if self.body_html:
            msg.add_alternative(self.body_html, subtype="html")
        for att in self.attachment_ids.elevated():
            maintype, _, subtype = (att.mimetype or "application/octet-stream").partition("/")
            msg.add_attachment(att.raw or b"", maintype=maintype, subtype=subtype or "octet-stream", filename=att.name)
        return msg

    def send_now(self) -> bool:
        self.ensure_one()
        if self.state not in ("outgoing", "failed"):
            return self.state == "sent"
        server = _server_for(self)
        msg = self.build_message()
        try:
            _deliver(server, msg)
        except Exception as exc:
            # recorded in its own transaction (the job's transaction rolls back), then re-raised so the
            # job retry policy applies
            from tessera.db.engine import Cursor

            cr = Cursor(self.env.cr.engine)
            try:
                cr.execute(
                    "UPDATE core_mail_queue SET state = 'failed', error = :e, attempts = COALESCE(attempts, 0) + 1"
                    " WHERE id = :id",
                    {"e": str(exc)[:2000], "id": self.ids[0]},
                )
                cr.commit()
            finally:
                cr.close()
            raise
        self.write({"state": "sent", "sent_at": self.env.now(), "error": None, "attempts": (self.attempts or 0) + 1})
        return True


def _mail_domain() -> str:
    sender = get_settings().mail_from
    return sender.rsplit("@", 1)[-1] if "@" in sender else "tessera.local"


def _html_to_text(html: str) -> str:
    import re

    text = re.sub(r"<(br|/p|/div|/li)[^>]*>", "\n", html, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _server_for(mail: Model) -> dict[str, Any]:
    servers = mail.env["core.mail_server"].elevated().search([("active", "=", True)], order="sequence, id")
    company = mail.company_id.ids[0] if mail.company_id else None
    for server in servers:
        if server.company_id and server.company_id.ids[0] != company:
            continue
        if server.from_filter and not mail.email_from.endswith(server.from_filter):
            continue
        return {
            "host": server.host,
            "port": server.port,
            "security": server.security,
            "username": server.username,
            "password": crypto.decrypt(server.password),
        }
    settings = get_settings()
    return {
        "host": settings.smtp_host,
        "port": settings.smtp_port,
        "security": "starttls" if settings.smtp_tls else "none",
        "username": settings.smtp_user,
        "password": settings.smtp_password,
    }


def _deliver(server: dict[str, Any], msg: EmailMessage) -> None:
    if server["security"] == "ssl":
        client: smtplib.SMTP = smtplib.SMTP_SSL(
            server["host"], server["port"], timeout=30, context=ssl.create_default_context()
        )
    else:
        client = smtplib.SMTP(server["host"], server["port"], timeout=30)
    with client:
        if server["security"] == "starttls":
            client.starttls(context=ssl.create_default_context())
        if server.get("username"):
            client.login(server["username"], server.get("password") or "")
        client.send_message(msg)


@job("core.send_mail", queue="mail", max_attempts=6, backoff_seconds=60, as_system=True)
def send_mail_job(env: Any, mail_id: int) -> str:
    mail = env["core.mail_queue"].browse([mail_id]).exists()
    if not mail:
        return "missing"
    mail.send_now()
    return "sent"
