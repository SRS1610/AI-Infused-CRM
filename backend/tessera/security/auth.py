"""Authentication helpers: sessions, API keys, TOTP and OIDC.

Sessions live in ``core.session`` (hashed token, idle + absolute expiry, CSRF token, TOTP state).
API keys live in ``core.api_key`` (``tsk_`` prefix, hashed). OIDC uses provider discovery, PKCE
and id-token validation against the provider's JWKS.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import secrets
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import pyotp

with warnings.catch_warnings():
    # authlib.jose is deprecated in favour of joserfc; the API we use is stable until authlib 2.0
    warnings.simplefilter("ignore", DeprecationWarning)
    from authlib.jose import JsonWebKey, JsonWebToken

from tessera.config import get_settings
from tessera.exceptions import AccessDenied, ConfigurationError, UserError
from tessera.logging import get_logger
from tessera.security.crypto import decrypt, hash_token, new_token

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)

API_KEY_PREFIX = "tsk_"


# -- sessions -----------------------------------------------------------------------------------


@dataclass
class SessionInfo:
    token: str
    session: Model
    csrf_token: str


def create_session(
    env: Environment, user: Model, *, ip: str | None, user_agent: str | None, totp_verified: bool
) -> SessionInfo:
    """``env`` must be elevated. Returns the plain token exactly once."""
    settings = get_settings()
    token = new_token(32)
    csrf = new_token(24)
    now = env.now()
    session = env["core.session"].create(
        {
            "user_id": user.ids[0],
            "token_hash": hash_token(token),
            "csrf_token": csrf,
            "ip": ip,
            "user_agent": (user_agent or "")[:500],
            "last_seen": now,
            "expires_at": now + dt.timedelta(hours=settings.session_absolute_hours),
            "totp_verified": totp_verified,
        }
    )
    return SessionInfo(token, session, csrf)


def resolve_session(env: Environment, token: str | None) -> Model | None:
    """Return the live session for ``token`` (touching ``last_seen``), or ``None``."""
    if not token:
        return None
    settings = get_settings()
    sessions = env["core.session"]
    session = sessions.search([("token_hash", "=", hash_token(token)), ("revoked", "=", False)], limit=1)
    if not session:
        return None
    now = env.now()
    idle_limit = now - dt.timedelta(minutes=settings.session_idle_minutes)
    if session.expires_at < now or session.last_seen < idle_limit:
        session.write({"revoked": True})
        return None
    if (now - session.last_seen).total_seconds() > 60:
        session.write({"last_seen": now})
    user = session.user_id
    if not user.exists() or not user.elevated().active:
        return None
    return session


def revoke_session(env: Environment, token: str | None) -> None:
    if not token:
        return
    env["core.session"].search([("token_hash", "=", hash_token(token))]).write({"revoked": True})


# -- API keys -----------------------------------------------------------------------------------


def generate_api_key() -> tuple[str, str, str]:
    """Return (plain key, prefix shown to the user, hash to store)."""
    raw = secrets.token_urlsafe(32)
    plain = API_KEY_PREFIX + raw
    return plain, plain[:12], hash_token(plain)


def resolve_api_key(env: Environment, plain: str | None) -> Model | None:
    """Return the ``core.api_key`` record for a valid, active, unexpired key."""
    if not plain or not plain.startswith(API_KEY_PREFIX):
        return None
    key = env["core.api_key"].search([("key_hash", "=", hash_token(plain)), ("active", "=", True)], limit=1)
    if not key:
        return None
    now = env.now()
    if key.expires_at and key.expires_at < now:
        return None
    if not key.user_id.exists() or not key.user_id.elevated().active:
        return None
    if not key.last_used_at or (now - key.last_used_at).total_seconds() > 60:
        key.write({"last_used_at": now})
    return key


# -- passwords ----------------------------------------------------------------------------------


def login_with_password(env: Environment, login: str, password: str) -> Model:
    """Validate credentials; raises ``AccessDenied``. ``env`` may be any environment."""
    user: Model = env["core.user"].authenticate(login, password)
    return user


# -- TOTP ---------------------------------------------------------------------------------------


def totp_new_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, account: str, issuer: str = "Tessera") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account, issuer_name=issuer)


def totp_verify(secret: str | None, code: str | None) -> bool:
    if not secret or not code:
        return False
    return bool(pyotp.TOTP(secret).verify(code.replace(" ", ""), valid_window=1))


# -- pre-auth tokens (between password and TOTP step) ---------------------------------------------

_PRE_AUTH_TTL = dt.timedelta(minutes=5)


def issue_pre_auth(env: Environment, user: Model) -> str:
    token = new_token(24)
    env["core.session"].create(
        {
            "user_id": user.ids[0],
            "token_hash": hash_token(token),
            "csrf_token": new_token(8),
            "pre_auth": True,
            "last_seen": env.now(),
            "expires_at": env.now() + _PRE_AUTH_TTL,
        }
    )
    return token


def consume_pre_auth(env: Environment, token: str) -> Model:
    session = env["core.session"].search(
        [("token_hash", "=", hash_token(token)), ("pre_auth", "=", True), ("revoked", "=", False)], limit=1
    )
    if not session or session.expires_at < env.now():
        raise AccessDenied("Second-factor step expired; log in again")
    session.write({"revoked": True})
    user: Model = session.user_id
    return user


# -- OIDC ---------------------------------------------------------------------------------------


@dataclass
class OidcTokens:
    claims: dict[str, Any]
    access_token: str | None


class OidcClient:
    """Minimal OpenID Connect relying party (authorization code + PKCE)."""

    def __init__(self, provider: Model) -> None:
        p = provider.elevated()
        self.name = p.name
        self.issuer = str(p.issuer).rstrip("/")
        self.client_id = str(p.client_id)
        self.client_secret = decrypt(p.client_secret) or ""
        self.scopes = str(p.scopes or "openid email profile")
        self._config: dict[str, Any] | None = None
        self.timeout = 10.0

    def discovery(self) -> dict[str, Any]:
        if self._config is None:
            resp = httpx.get(f"{self.issuer}/.well-known/openid-configuration", timeout=self.timeout)
            resp.raise_for_status()
            self._config = resp.json()
        return self._config

    @staticmethod
    def pkce_pair() -> tuple[str, str]:
        verifier = secrets.token_urlsafe(48)
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
        return verifier, challenge

    def authorization_url(self, redirect_uri: str, state: str, nonce: str, code_challenge: str) -> str:
        cfg = self.discovery()
        params = httpx.QueryParams(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": self.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{cfg['authorization_endpoint']}?{params}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str, nonce: str) -> OidcTokens:
        cfg = self.discovery()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.client_id,
            "code_verifier": code_verifier,
        }
        if self.client_secret:
            data["client_secret"] = self.client_secret
        resp = httpx.post(cfg["token_endpoint"], data=data, timeout=self.timeout)
        if resp.status_code >= 400:
            raise AccessDenied(f"Identity provider rejected the login ({resp.status_code})")
        tokens = resp.json()
        id_token = tokens.get("id_token")
        if not id_token:
            raise AccessDenied("Identity provider returned no id_token")
        claims = self.validate_id_token(id_token, nonce)
        return OidcTokens(claims=dict(claims), access_token=tokens.get("access_token"))

    def validate_id_token(self, id_token: str, nonce: str) -> dict[str, Any]:
        cfg = self.discovery()
        jwks_resp = httpx.get(cfg["jwks_uri"], timeout=self.timeout)
        jwks_resp.raise_for_status()
        key_set = JsonWebKey.import_key_set(jwks_resp.json())
        jwt = JsonWebToken(cfg.get("id_token_signing_alg_values_supported", ["RS256"]))
        claims = jwt.decode(
            id_token,
            key_set,
            claims_options={
                "iss": {"essential": True, "values": [cfg["issuer"], self.issuer]},
                "aud": {"essential": True, "value": self.client_id},
                "nonce": {"essential": True, "value": nonce},
            },
        )
        claims.validate(leeway=60)
        return dict(claims)


def user_for_oidc_claims(env: Environment, provider: Model, claims: dict[str, Any]) -> Model:
    """Find or provision the user for validated OIDC claims (``env`` must be elevated)."""
    email = (claims.get("email") or "").strip().lower()
    subject = claims.get("sub")
    if not subject:
        raise AccessDenied("Identity token has no subject")
    users = env["core.user"].with_context(active_test=False)
    user = users.search([("sso_provider_id", "=", provider.ids[0]), ("sso_subject", "=", subject)], limit=1)
    if not user and email:
        user = users.search([("login", "=", email)], limit=1)
        if user:
            user.write({"sso_provider_id": provider.ids[0], "sso_subject": subject})
    if not user:
        if not provider.auto_create_users:
            raise AccessDenied("No account matches this identity; ask an administrator to invite you")
        if not email:
            raise AccessDenied("Identity token has no email; cannot create an account")
        vals: dict[str, Any] = {
            "login": email,
            "email": email,
            "name": claims.get("name") or email,
            "sso_provider_id": provider.ids[0],
            "sso_subject": subject,
            "company_id": provider.default_company_id.ids[0] if provider.default_company_id else env.company.ids[0],
        }
        if provider.default_group_id:
            vals["group_ids"] = [("set", [provider.default_group_id.ids[0]])]
        user = users.create(vals)
    if not user.active:
        raise AccessDenied("This account is disabled")
    return user


def ensure_settings_for_auth() -> None:
    settings = get_settings()
    if settings.is_production and len(settings.secret_key) < 32:
        raise ConfigurationError("TESSERA_SECRET_KEY too short")


def check_password_policy(password: str) -> None:
    if len(password) < 8:
        raise UserError("Password must be at least 8 characters")
