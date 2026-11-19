"""SAML login flow: login requests bound to the browser, response consumption and account mapping.

Every function takes an elevated environment (the login endpoints run before anyone is authenticated)."""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from modules.auth_saml import saml
from tessera.exceptions import AccessDenied
from tessera.i18n import _
from tessera.jobs import job
from tessera.logging import get_logger
from tessera.security.crypto import constant_time_equals, hash_token, new_token

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)

REQUEST_TTL = dt.timedelta(minutes=10)
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@dataclass
class LoginStart:
    redirect_url: str
    binding_token: str  # goes into the browser-binding cookie, never into the URL


def sp_config(provider: Model) -> saml.SpConfig:
    p = provider.elevated("saml configuration")
    return saml.SpConfig(
        idp_entity_id=str(p.idp_entity_id).strip(),
        idp_certificates=saml.load_certificates(p.idp_certificates),
        sp_entity_id=p.entity_id(),
        acs_url=p.acs(),
        clock_skew=int(p.clock_skew or 0),
        want_assertions_signed=bool(p.want_assertions_signed),
    )


def start_login(env: Environment, provider: Model) -> LoginStart:
    """Record a pending AuthnRequest bound to a new browser token and build the IdP redirect."""
    now = env.now()
    request_id = saml.new_message_id()
    binding, relay = new_token(24), new_token(24)
    env["auth_saml.request"].create(
        {
            "provider_id": provider.ids[0],
            "request_id": request_id,
            "binding_hash": hash_token(binding),
            "relay_hash": hash_token(relay),
            "expires_at": now + REQUEST_TTL,
        }
    )
    xml = saml.build_authn_request(
        request_id=request_id,
        sp_entity_id=provider.entity_id(),
        acs_url=provider.acs(),
        destination=str(provider.idp_sso_url),
        name_id_format=saml.NAMEID_FORMATS.get(str(provider.name_id_format or "")),
        now=now,
    )
    key = provider.private_key() if provider.sign_requests else None
    return LoginStart(saml.redirect_url(str(provider.idp_sso_url), xml, relay, signing_key=key), binding)


def consume_response(
    env: Environment,
    provider: Model,
    saml_response: str,
    relay_state: str,
    binding_token: str | None,
    *,
    now: dt.datetime | None = None,
) -> tuple[Model, saml.ValidatedAssertion]:
    """Validate a response for ``provider`` posted by the browser holding ``binding_token``; return the user.

    The pending request is used up and the assertion id recorded (both race-safe) before the account is looked
    up. Raises :class:`saml.SamlError` (protocol) or ``AccessDenied`` (account)."""
    now = now or env.now()
    xml = saml.decode_post_response(saml_response)
    if not binding_token:
        raise saml.SamlError("no_binding", _("Login session expired; start again"))
    pending = env["auth_saml.request"].search(
        [
            ("provider_id", "=", provider.ids[0]),
            ("binding_hash", "=", hash_token(binding_token)),
            ("used", "=", False),
            ("expires_at", ">", now),
        ]
    )
    if not pending:
        raise saml.SamlError("no_pending_request", _("Login session expired; start again"))
    assertion = saml.validate_response(
        xml, sp_config(provider), expected_request_ids=[str(r) for r in pending.mapped("request_id")], now=now
    )
    request = pending.filtered(lambda r: r.request_id == assertion.in_response_to)
    if not request or not constant_time_equals(str(request.relay_hash), hash_token(relay_state or "")):
        raise saml.SamlError("relay_state", _("Login session expired; start again"))
    env.flush()
    # one use: a second response for the same request (or a concurrent one) finds it used
    used = env.cr.scalar(
        "UPDATE auth_saml_request SET used = true WHERE id = :id AND used IS NOT TRUE RETURNING id",
        {"id": request.ids[0]},
    )
    if not used:
        raise saml.SamlError("request_used", _("This login was already completed; start again"))
    # replay cache: the unique index makes a concurrent replay wait for this transaction, then fail
    recorded = env.cr.scalar(
        "INSERT INTO auth_saml_assertion (provider_id, assertion_id, expires_at) VALUES (:p, :a, :e) "
        "ON CONFLICT (provider_id, assertion_id) DO NOTHING RETURNING id",
        {"p": provider.ids[0], "a": assertion.assertion_id[:256], "e": assertion.valid_until},
    )
    if not recorded:
        raise saml.SamlError("replay", _("This assertion was already used; sign in again"))
    return user_for_assertion(env, provider, assertion, now=now), assertion


def _email_of(provider: Model, assertion: saml.ValidatedAssertion) -> str | None:
    raw = assertion.attribute(str(provider.email_attribute)) if provider.email_attribute else assertion.name_id
    email = (raw or "").strip().lower()
    return email if _EMAIL.match(email) and len(email) <= 128 else None


def user_for_assertion(
    env: Environment, provider: Model, assertion: saml.ValidatedAssertion, *, now: dt.datetime | None = None
) -> Model:
    """Find (or provision) the account for a validated assertion.

    1. an existing link (provider, NameID);
    2. else the account whose login is the asserted e-mail address — only when the provider is trusted for
       e-mail addresses (``trust_email``); otherwise the administrator links the account;
    3. else a new internal account when the provider allows sign-up."""
    now = now or env.now()
    subject = assertion.name_id
    links = env["auth_saml.identity"]
    users = env["core.user"].with_context(active_test=False)
    link = links.search([("provider_id", "=", provider.ids[0]), ("subject", "=", subject)], limit=1)
    if link:
        user = link.user_id
    else:
        email = _email_of(provider, assertion)
        user = users.search([("login", "=", email)], limit=1) if email else users.browse([])
        if user and not provider.trust_email:
            raise AccessDenied(
                _(
                    "An account with this e-mail address exists but is not linked to this identity provider; "
                    "ask an administrator to link it"
                ),
                reason="link_required",
            )
        if not user:
            if not provider.allow_signup:
                raise AccessDenied(
                    _("No account matches this identity; ask an administrator to invite you or link your account"),
                    reason="no_account",
                )
            if not email:
                raise AccessDenied(
                    _("The identity provider sent no e-mail address; cannot create an account"), reason="no_email"
                )
            user = _create_user(env, provider, assertion, email)
        link = links.create({"provider_id": provider.ids[0], "subject": subject, "user_id": user.ids[0]})
    if not user.active:
        raise AccessDenied(_("This account is disabled"), reason="disabled")
    if provider.company_id:
        allowed = set(user.company_ids.ids) | set(user.company_id.ids)
        if provider.company_id.ids[0] not in allowed:
            raise AccessDenied(_("This identity provider cannot sign in accounts of another company"), reason="company")
    link.write({"last_login": now})
    user.with_context(_recompute_write=True)._write_columns({"last_login": now})
    result: Model = user
    return result


def _create_user(env: Environment, provider: Model, assertion: saml.ValidatedAssertion, email: str) -> Model:
    """A new internal user with the default internal-user group only (never more)."""
    internal = env.ref("core.group_user")
    assert internal is not None
    name = assertion.attribute(str(provider.name_attribute)) if provider.name_attribute else None
    company_id = provider.company_id.ids[0] if provider.company_id else env.company.ids[0]
    vals: dict[str, Any] = {
        "login": email,
        "email": email,
        "name": (name or email)[:200],
        "company_id": company_id,
        "company_ids": [("set", [company_id])],
        "group_ids": [("set", internal.ids)],
    }
    user: Model = env["core.user"].create(vals)
    log.info("saml account created", provider_id=provider.ids[0], user_id=user.ids[0])
    return user


@job("auth_saml.purge", queue="default", max_attempts=1, as_system=True)
def purge_expired(env: Environment) -> dict[str, int]:
    """Drop answered or expired login requests and expired replay-cache entries."""
    now = env.now()
    requests = env["auth_saml.request"].search({"any": [["used", "=", True], ["expires_at", "<", now]]})
    assertions = env["auth_saml.assertion"].search([("expires_at", "<", now)])
    counts = {"requests": len(requests), "assertions": len(assertions)}
    requests.unlink()
    assertions.unlink()
    return counts
