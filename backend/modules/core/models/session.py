"""Sessions, API keys and SSO providers."""

from __future__ import annotations

from tessera.orm import Model, api, fields
from tessera.security import crypto


class Session(Model):
    _name = "core.session"
    _description = "Login session"
    _order = "id desc"
    _rec_name = "user_id"
    _studio = False
    _log_access = False

    user_id = fields.Many2one("core.user", required=True, ondelete="cascade", index=True)
    token_hash = fields.Char(required=True, index="unique", size=64, groups="core.group_system")
    csrf_token = fields.Char(required=True, groups="core.group_system")
    pre_auth = fields.Boolean(default=False, help="Password accepted, second factor pending")
    totp_verified = fields.Boolean(default=False)
    revoked = fields.Boolean(default=False, index=True)
    ip = fields.Char(size=64)
    user_agent = fields.Char(size=512)
    last_seen = fields.Datetime(required=True, default=lambda self: self.env.now())
    expires_at = fields.Datetime(required=True, index=True)
    created_at = fields.Datetime(default=lambda self: self.env.now())


class ApiKey(Model):
    _name = "core.api_key"
    _description = "API key"
    _rule_check_after_write = True  # owner-scoped: cannot be handed to another user
    _order = "id desc"
    _audit = True

    name = fields.Char(required=True)
    user_id = fields.Many2one(
        "core.user", required=True, ondelete="cascade", index=True, default=lambda self: self.env.uid
    )
    key_prefix = fields.Char(readonly=True, help="First characters, to recognise the key")
    key_hash = fields.Char(readonly=True, index="unique", size=64, groups="core.group_system")
    scopes = fields.Json(default=list, help='e.g. ["read", "write"]; empty = same rights as the user')
    expires_at = fields.Datetime()
    last_used_at = fields.Datetime(readonly=True)
    active = fields.Boolean(default=True)

    @api.expose
    def action_regenerate(self) -> dict[str, str]:
        """Issue a new secret for this key; the plain value is returned exactly once."""
        self.ensure_one()
        from tessera.security.auth import generate_api_key

        plain, prefix, digest = generate_api_key()
        self.elevated("api key").with_context(_recompute_write=True)._write_columns(
            {"key_prefix": prefix, "key_hash": digest}
        )
        return {"key": plain, "prefix": prefix}

    def allows(self, scope: str) -> bool:
        self.ensure_one()
        scopes = list(self.scopes or [])
        return not scopes or scope in scopes or "*" in scopes


class SsoProvider(Model):
    _name = "core.sso_provider"
    _description = "Single sign-on provider (OIDC)"
    _order = "sequence, name"
    _audit = True
    _sql_constraints = [("code_uniq", "UNIQUE (code)", "Provider codes must be unique")]

    name = fields.Char(required=True)
    code = fields.Char(required=True, size=32, index=True, help="Used in the login URL, e.g. 'okta'")
    kind = fields.Selection([("oidc", "OpenID Connect")], default="oidc", required=True)
    issuer = fields.Char(required=True, help="Issuer URL exposing /.well-known/openid-configuration")
    client_id = fields.Char(required=True)
    client_secret = fields.Char(groups="core.group_system", help="Stored encrypted")
    scopes = fields.Char(default="openid email profile")
    auto_create_users = fields.Boolean(default=False)
    default_group_id = fields.Many2one("core.group", ondelete="set_null")
    default_company_id = fields.Many2one("core.company", ondelete="set_null")
    enabled = fields.Boolean(default=True)
    sequence = fields.Integer(default=10)
    button_label = fields.Char(help="Text on the login button; defaults to the name")

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("client_secret") and not crypto.is_encrypted(vals["client_secret"]):
                vals["client_secret"] = crypto.encrypt(vals["client_secret"])
        return super().create(batch)

    def write(self, vals):  # type: ignore[no-untyped-def]
        vals = dict(vals)
        if vals.get("client_secret") and not crypto.is_encrypted(vals["client_secret"]):
            vals["client_secret"] = crypto.encrypt(vals["client_secret"])
        return super().write(vals)


class UserSso(Model):
    _extend = "core.user"

    sso_provider_id = fields.Many2one("core.sso_provider", ondelete="set_null")
    sso_subject = fields.Char(index=True, groups="core.group_system")
    api_key_ids = fields.One2many("core.api_key", "user_id")
    session_ids = fields.One2many("core.session", "user_id")
