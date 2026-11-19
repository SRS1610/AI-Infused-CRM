"""SAML identity providers, account links, pending login requests and the assertion replay cache."""

from __future__ import annotations

import urllib.parse
from typing import Any

from modules.auth_saml import saml
from tessera.config import get_settings
from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields
from tessera.security import crypto

SECRET_FIELDS = ("sp_private_key",)


def _encrypt_secrets(vals: dict[str, Any]) -> dict[str, Any]:
    for name in SECRET_FIELDS:
        if vals.get(name) and not crypto.is_encrypted(vals[name]):
            vals[name] = crypto.encrypt(vals[name])
    return vals


class Provider(Model):
    _name = "auth_saml.provider"
    _description = "SAML identity provider"
    _order = "sequence, name"
    _audit = True
    _studio = False

    name = fields.Char(required=True, help="Shown on the login button")
    active = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one(
        "core.company",
        ondelete="restrict",
        help="When set, only users of this company can sign in with this provider, and new accounts are created in it",
    )
    idp_entity_id = fields.Char("IdP entity ID", required=True, help="The identity provider's entityID (its Issuer)")
    idp_sso_url = fields.Char(
        "IdP single sign-on URL", required=True, help="SingleSignOnService location for the HTTP-Redirect binding"
    )
    idp_certificates = fields.Text(
        "IdP signing certificates",
        required=True,
        help="PEM certificate(s) the identity provider signs with; paste the new one next to the old one "
        "during a key rotation. Certificates carried in messages are never trusted",
    )
    sp_entity_id = fields.Char(
        "SP entity ID", help="This application's entityID; defaults to the metadata URL. Change it with care"
    )
    name_id_format = fields.Selection(
        [("unspecified", "Unspecified"), ("email", "E-mail address"), ("persistent", "Persistent")],
        "NameID format",
        default="unspecified",
        required=True,
        help="Requested in the login request; transient NameIDs are refused",
    )
    email_attribute = fields.Char(
        "E-mail attribute",
        help="Attribute holding the e-mail address; empty: the NameID is the e-mail address",
    )
    name_attribute = fields.Char(
        "Display name attribute", default="displayName", help="Attribute holding the person's name for new accounts"
    )
    allow_signup = fields.Boolean(
        "Create accounts on first login",
        default=False,
        help="Unknown people get a new internal account with the default internal-user groups only",
    )
    trust_email = fields.Boolean(
        "Link accounts by e-mail",
        default=False,
        help="Link a first login to the existing account whose login is the asserted e-mail address. Only turn "
        "this on when the identity provider guarantees the address belongs to the person (it is the address of "
        "your own directory); otherwise an administrator links accounts explicitly",
    )
    want_assertions_signed = fields.Boolean(
        "Require signed assertions",
        default=True,
        help="Refuse responses whose assertion is only covered by the response signature",
    )
    sign_requests = fields.Boolean("Sign login requests", default=False)
    sp_certificate = fields.Text("SP certificate", help="PEM certificate published in the metadata")
    sp_private_key = fields.Text(
        "SP private key",
        groups="core.group_system",
        copy=False,
        help="PEM key signing login requests; stored encrypted",
    )
    clock_skew = fields.Integer("Clock skew (seconds)", default=120, required=True)
    acs_url = fields.Char("Assertion consumer service URL", compute="_compute_urls")
    metadata_url = fields.Char("Metadata URL", compute="_compute_urls")
    identity_ids = fields.One2many("auth_saml.identity", "provider_id", "Linked accounts")

    _sql_constraints = [
        ("clock_skew_range", "CHECK (clock_skew BETWEEN 0 AND 600)", "Clock skew must be between 0 and 600 seconds")
    ]

    # -- URLs -------------------------------------------------------------------------------------------

    @staticmethod
    def _base_url(provider_id: int) -> str:
        return f"{get_settings().public_url.rstrip('/')}/api/v1/auth/saml/{provider_id}"

    @api.depends("sp_entity_id")
    def _compute_urls(self) -> None:
        for provider in self:
            pid = provider.ids[0] if provider.ids else 0
            provider.acs_url = f"{self._base_url(pid)}/acs"
            provider.metadata_url = f"{self._base_url(pid)}/metadata"

    def entity_id(self) -> str:
        self.ensure_one()
        return str(self.sp_entity_id or f"{self._base_url(self.ids[0])}/metadata")

    def acs(self) -> str:
        self.ensure_one()
        return f"{self._base_url(self.ids[0])}/acs"

    # -- secrets ----------------------------------------------------------------------------------------

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        records = super().create([_encrypt_secrets(vals) for vals in batch])
        for provider in records:
            if not provider.sp_entity_id:
                provider.write({"sp_entity_id": f"{self._base_url(provider.ids[0])}/metadata"})
        return records

    def write(self, vals):  # type: ignore[no-untyped-def]
        return super().write(_encrypt_secrets(dict(vals)))

    def private_key(self) -> Any:
        """The decrypted SP signing key, or ``None``."""
        self.ensure_one()
        pem = crypto.decrypt(self.elevated("saml signing key").sp_private_key)
        return saml.load_private_key(pem) if pem else None

    def signing_certificate(self) -> Any:
        self.ensure_one()
        certs = saml.load_certificates(self.sp_certificate)
        return certs[0] if certs else None

    # -- validation -------------------------------------------------------------------------------------

    @api.constrains("idp_certificates")
    def _check_idp_certificates(self) -> None:
        for provider in self:
            try:
                certs = saml.load_certificates(provider.idp_certificates)
            except saml.SamlError as exc:
                raise ValidationError(exc.message, field="idp_certificates") from exc
            if not certs:
                raise ValidationError(
                    _("Paste at least one certificate of the identity provider"), field="idp_certificates"
                )

    @api.constrains("idp_sso_url")
    def _check_idp_sso_url(self) -> None:
        for provider in self:
            parts = urllib.parse.urlsplit(str(provider.idp_sso_url or ""))
            allowed = ("https",) if get_settings().is_production else ("https", "http")
            if parts.scheme not in allowed or not parts.netloc or parts.fragment:
                raise ValidationError(_("The single sign-on URL must be an https:// address"), field="idp_sso_url")

    @api.constrains("sign_requests", "sp_certificate", "sp_private_key")
    def _check_signing_material(self) -> None:
        for provider in self:
            sp = provider.elevated("saml signing key")
            certs = []
            if sp.sp_certificate:
                try:
                    certs = saml.load_certificates(sp.sp_certificate)
                except saml.SamlError as exc:
                    raise ValidationError(exc.message, field="sp_certificate") from exc
            if not sp.sign_requests and not sp.sp_private_key:
                continue
            if not certs or not sp.sp_private_key:
                raise ValidationError(
                    _("Signing login requests needs the SP certificate and private key (or generate a pair)"),
                    field="sp_private_key",
                )
            try:
                key = sp.private_key()
            except saml.SamlError as exc:
                raise ValidationError(exc.message, field="sp_private_key") from exc
            if not saml.key_matches_certificate(key, certs[0]):
                raise ValidationError(
                    _("The SP private key does not belong to the SP certificate"), field="sp_private_key"
                )

    # -- actions ----------------------------------------------------------------------------------------

    @api.expose
    def action_generate_sp_keypair(self) -> bool:
        """Generate a new key and self-signed certificate for signing login requests; the identity provider must
        then be given the new metadata."""
        for provider in self:
            host = urllib.parse.urlsplit(provider.entity_id()).hostname or "tessera"
            key_pem, cert_pem = saml.generate_sp_keypair(f"Tessera SAML SP {host}")
            provider.write({"sp_private_key": key_pem, "sp_certificate": cert_pem, "sign_requests": True})
        return True


class Identity(Model):
    _name = "auth_saml.identity"
    _description = "SAML account link"
    _order = "provider_id, subject"
    _rec_name = "subject"
    _audit = True
    _studio = False
    _sql_constraints = [
        ("subject_uniq", "UNIQUE (provider_id, subject)", "This identity is already linked to an account")
    ]

    provider_id = fields.Many2one("auth_saml.provider", required=True, ondelete="cascade")
    subject = fields.Char("Subject (NameID)", required=True, help="The NameID the identity provider sends")
    user_id = fields.Many2one("core.user", required=True, ondelete="cascade")
    last_login = fields.Datetime(readonly=True)


class LoginRequest(Model):
    """An AuthnRequest sent to an identity provider and not answered yet (one use, short-lived)."""

    _name = "auth_saml.request"
    _description = "Pending SAML login"
    _order = "id desc"
    _rec_name = "request_id"
    _log_access = False
    _studio = False

    provider_id = fields.Many2one("auth_saml.provider", required=True, ondelete="cascade")
    request_id = fields.Char(required=True, index="unique", size=64)
    binding_hash = fields.Char(required=True, size=64, help="SHA-256 of the browser-binding cookie")
    relay_hash = fields.Char(required=True, size=64, help="SHA-256 of the RelayState")
    expires_at = fields.Datetime(required=True, index=True)
    used = fields.Boolean(default=False)


class SeenAssertion(Model):
    """Replay cache: identifiers of accepted assertions, kept until they expire."""

    _name = "auth_saml.assertion"
    _description = "Accepted SAML assertion"
    _order = "id desc"
    _rec_name = "assertion_id"
    _log_access = False
    _studio = False
    _sql_constraints = [("assertion_uniq", "UNIQUE (provider_id, assertion_id)", "Assertion already used")]

    provider_id = fields.Many2one("auth_saml.provider", required=True, ondelete="cascade")
    assertion_id = fields.Char(required=True, size=256)
    expires_at = fields.Datetime(required=True, index=True)
