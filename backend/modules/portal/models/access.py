"""Portal access: invite a contact, let them set a password, revoke."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from typing import Any

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields

INVITATION_VALIDITY = dt.timedelta(days=7)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class PortalAccess(Model):
    _name = "portal.access"
    _description = "Portal access"
    _order = "id desc"
    _rec_name = "partner_id"
    _audit = True

    partner_id = fields.Many2one("contacts.partner", "Contact", required=True, ondelete="cascade", index=True)
    user_id = fields.Many2one("core.user", "Portal user", ondelete="set_null")
    state = fields.Selection(
        [("invited", "Invited"), ("active", "Active"), ("revoked", "Revoked")], default="invited", required=True
    )
    invitation_token_hash = fields.Char(groups="core.group_system", copy=False)
    invited_at = fields.Datetime(readonly=True)
    invitation_expires_at = fields.Datetime(readonly=True)
    activated_at = fields.Datetime(readonly=True)
    revoked_at = fields.Datetime(readonly=True)


class Partner(Model):
    _extend = "contacts.partner"

    portal_access_ids = fields.One2many("portal.access", "partner_id", "Portal access")
    has_portal_access = fields.Boolean(compute="_compute_has_portal_access", store=True)

    @api.depends("portal_access_ids.state")
    def _compute_has_portal_access(self) -> None:
        for p in self:
            p.has_portal_access = any(a.state == "active" for a in p.portal_access_ids)

    @api.expose
    def action_grant_portal(self) -> dict[str, Any]:
        """Create (or reuse) a portal user for this contact and email them a single-use invitation."""
        from tessera.config import get_settings

        self.ensure_one()
        self.check_access("write")
        me = self.elevated("portal invitation")
        if not me.email_normalized:
            raise UserError(_("The contact needs an email address to receive the invitation"))
        users = self.env["core.user"].elevated("portal invitation").with_context(active_test=False)
        user = users.search([("partner_id", "=", self.ids[0])], limit=1)
        if user and not user.share:
            raise UserError(_("This contact is already an internal user"))
        portal_group = self.env.ref("core.group_portal")
        if not user:
            if users.search_count([("login", "=", me.email_normalized)]):
                raise UserError(
                    _("A user with the login {email_normalized} already exists", email_normalized=me.email_normalized)
                )
            user = users.create(
                {
                    "login": me.email_normalized,
                    "name": me.name or me.email_normalized,
                    "email": me.email_normalized,
                    "partner_id": self.ids[0],
                    "group_ids": [("set", portal_group.ids)],
                    "active": False,
                    "company_id": (self.env.company or self.env.ref("core.main_company")).ids[0],
                }
            )
        token = secrets.token_urlsafe(32)
        now = self.env.now()
        accesses = self.env["portal.access"].elevated()
        access = accesses.search([("partner_id", "=", self.ids[0])], limit=1)
        vals = {
            "user_id": user.ids[0],
            "state": "invited",
            "invitation_token_hash": token_hash(token),
            "invited_at": now,
            "invitation_expires_at": now + INVITATION_VALIDITY,
            "revoked_at": None,
        }
        if access:
            access.write(vals)
        else:
            access = accesses.create({"partner_id": self.ids[0], **vals})
        link = f"{get_settings().public_url.rstrip('/')}/portal/invite?token={token}"
        company = self.env.company or self.env.ref("core.main_company")
        self.env["core.mail_queue"].queue(
            {
                "email_to": me.email_normalized,
                "subject": f"Your {company.name} customer portal",
                "body_html": (
                    f"<p>Hello {me.name},</p><p>You have been invited to the {company.name} customer portal, "
                    "where you can review and accept quotations and keep your details up to date.</p>"
                    f'<p><a href="{link}">Set your password</a> '
                    "(the link is valid for 7 days and can be used once).</p>"
                ),
                "model": "contacts.partner",
                "res_id": self.ids[0],
            }
        )
        return {"access_id": access.ids[0], "user_id": user.ids[0], "invitation_url": link}

    @api.expose
    def action_revoke_portal(self) -> bool:
        self.check_access("write")
        for partner in self.elevated("portal revoke"):
            for access in partner.portal_access_ids.filtered(lambda a: a.state != "revoked"):
                access.write({"state": "revoked", "revoked_at": self.env.now(), "invitation_token_hash": None})
                if access.user_id:
                    access.user_id.write({"active": False})
                    self.env["core.session"].elevated().search([("user_id", "=", access.user_id.ids[0])]).unlink()
        return True


def accept_invitation(env: Any, token: str, password: str) -> Model:
    """Activate the portal user behind a valid, unused, unexpired invitation token."""
    if len(password or "") < 8:
        raise UserError(_("Choose a password of at least 8 characters"))
    accesses = env["portal.access"].elevated("portal invitation")
    access = accesses.search([("invitation_token_hash", "=", token_hash(token)), ("state", "=", "invited")], limit=1)
    if not access:
        raise UserError(_("This invitation link is invalid or was already used"))
    if access.invitation_expires_at and access.invitation_expires_at < env.now():
        raise UserError(_("This invitation has expired; ask for a new one"))
    user = access.user_id.with_context(active_test=False)
    user.write({"active": True})
    user.set_password(password)
    access.write({"state": "active", "activated_at": env.now(), "invitation_token_hash": None})
    return user
