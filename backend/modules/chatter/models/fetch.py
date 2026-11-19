"""IMAP mailboxes polled for inbound email, and the daily notification digest."""

from __future__ import annotations

import contextlib
import imaplib
from typing import Any

from tessera.jobs import job
from tessera.orm import Model, api, fields
from tessera.security import crypto


class FetchServer(Model):
    _name = "chatter.fetch_server"
    _description = "Incoming mail server (IMAP)"
    _order = "name"
    _audit = True

    name = fields.Char(required=True)
    host = fields.Char(required=True)
    port = fields.Integer(default=993)
    use_ssl = fields.Boolean("SSL", default=True)
    username = fields.Char(required=True)
    password = fields.Char(groups="core.group_system", help="Stored encrypted")
    folder = fields.Char(default="INBOX")
    active = fields.Boolean(default=True)
    last_fetch = fields.Datetime(readonly=True)
    last_error = fields.Text(readonly=True)

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

    def _connect(self) -> imaplib.IMAP4:
        cls = imaplib.IMAP4_SSL if self.use_ssl else imaplib.IMAP4
        conn = cls(self.host, self.port)
        conn.login(self.username, crypto.decrypt(self.elevated().password) or "")
        return conn

    def fetch(self, limit: int = 50) -> dict[str, int]:
        """Process unseen messages; each is marked seen only after it was processed."""
        from modules.chatter.inbound import process_email

        self.ensure_one()
        counts = {"processed": 0, "failed": 0}
        conn = self._connect()
        try:
            conn.select(self.folder or "INBOX")
            _, data = conn.search(None, "UNSEEN")
            for num in (data[0] or b"").split()[:limit]:
                _, parts = conn.fetch(num, "(BODY.PEEK[])")
                raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
                if raw is None:
                    continue
                try:
                    with self.env.cr.savepoint():
                        process_email(self.env, raw)
                    counts["processed"] += 1
                except Exception as exc:  # one bad email never blocks the mailbox
                    counts["failed"] += 1
                    self.write({"last_error": str(exc)[:2000]})
                conn.store(num, "+FLAGS", "\\Seen")
        finally:
            with contextlib.suppress(Exception):  # best effort
                conn.logout()
        self.write({"last_fetch": self.env.now()})
        return counts

    @api.expose
    def action_fetch_now(self) -> dict[str, int]:
        return self.fetch()


@job("chatter.fetch_mail", queue="mail", max_attempts=1, as_system=True)
def fetch_mail_job(env: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for server in env["chatter.fetch_server"].search([("active", "=", True)]):
        try:
            out[server.name] = server.fetch()
        except Exception as exc:
            server.write({"last_error": str(exc)[:2000]})
            out[server.name] = {"error": str(exc)[:200]}
    return out


@job("chatter.digest", queue="mail", max_attempts=3, as_system=True)
def digest_job(env: Any) -> dict[str, int]:
    """One email per opted-in user listing unread notifications not yet included in a digest."""
    import html

    sent = 0
    users = env["core.user"].search([("notification_digest", "=", True), ("email", "!=", None)])
    notifications = env["core.notification"]
    for user in users:
        pending = notifications.search(
            [("user_id", "=", user.ids[0]), ("is_read", "=", False), ("digested", "=", False)], order="id", limit=100
        )
        if not pending:
            continue
        items = "".join(f"<li><b>{html.escape(n.title)}</b><br/>{html.escape(n.body or '')}</li>" for n in pending)
        env["core.mail_queue"].queue(
            {
                "email_to": user.email,
                "subject": f"Your Tessera digest: {len(pending)} unread notification(s)",
                "body_html": f"<p>Hello {html.escape(user.name)},</p><ul>{items}</ul>",
            }
        )
        pending.write({"digested": True})
        sent += 1
    return {"digests": sent}


class User(Model):
    _extend = "core.user"

    notification_digest = fields.Boolean("Daily email digest", default=False)


class Notification(Model):
    _extend = "core.notification"

    digested = fields.Boolean(default=False)
