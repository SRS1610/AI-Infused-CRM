"""SAML endpoints: public provider list, login start (HTTP-Redirect), assertion consumer (HTTP-POST), metadata.

The login start sets a short-lived, HttpOnly browser-binding cookie scoped to these paths; the assertion
consumer only accepts a response that answers a pending request issued to the browser holding it. With secure
cookies (production) the cookie is ``SameSite=None; Secure`` so the identity provider's cross-site form post
carries it; in development it is ``SameSite=Lax`` (enough for an identity provider on the same site)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from modules.auth_saml import saml, service
from tessera.api.deps import RequestContext, exempt_login_path, request_context
from tessera.api.middleware import register_login_path
from tessera.api.registry import register_router
from tessera.api.routers.auth import _finish_login
from tessera.config import get_settings
from tessera.exceptions import AccessDenied, MissingError
from tessera.i18n import _
from tessera.logging import get_logger
from tessera.runtime import current_remote_ip

log = get_logger(__name__)
router = register_router(APIRouter(prefix="/auth/saml", tags=["auth"]))

BINDING_COOKIE = "tessera_saml"
COOKIE_PATH = "/api/v1/auth/saml/"
MAX_FORM_FIELD = saml.MAX_RESPONSE_BYTES * 4 // 3 + 4

register_login_path(COOKIE_PATH)  # the assertion consumer is a login endpoint: same per-address limit
exempt_login_path(r"/api/v1/auth/saml/\d+/acs")  # posted by the IdP's page: no CSRF header, never uses a session


def _is_admin(ctx: RequestContext) -> bool:
    return ctx.authenticated and ctx.registry.security.has_groups(ctx.env, ["core.group_system"])


def _provider(ctx: RequestContext, provider_id: int, *, include_inactive: bool = False) -> Any:
    provider = ctx.system_env()["auth_saml.provider"].with_context(active_test=False).browse([provider_id]).exists()
    if not provider or (not provider.active and not include_inactive):
        raise MissingError(_("Unknown identity provider"))
    return provider


def _set_binding_cookie(response: Response, value: str) -> None:
    secure = get_settings().secure_cookies
    response.set_cookie(
        BINDING_COOKIE,
        value,
        httponly=True,
        secure=secure,
        samesite="none" if secure else "lax",
        max_age=int(service.REQUEST_TTL.total_seconds()),
        path=COOKIE_PATH,
    )


@router.get("/providers")
def list_providers(ctx: RequestContext = Depends(request_context)) -> list[dict[str, Any]]:
    """Active providers for the login page (no configuration is disclosed)."""
    providers = ctx.system_env()["auth_saml.provider"].search([])
    return [{"id": p.ids[0], "name": p.name} for p in providers]


@router.get("/{provider_id}/login")
def saml_login(provider_id: int, ctx: RequestContext = Depends(request_context)) -> Response:
    provider = _provider(ctx, provider_id)
    start = service.start_login(ctx.system_env(), provider)
    response = RedirectResponse(start.redirect_url, status_code=302)
    _set_binding_cookie(response, start.binding_token)
    return response


@router.post("/{provider_id}/acs")
def saml_acs(
    provider_id: int,
    request: Request,
    saml_response: str = Form("", alias="SAMLResponse", max_length=MAX_FORM_FIELD),
    relay_state: str = Form("", alias="RelayState", max_length=256),
    ctx: RequestContext = Depends(request_context),
) -> Response:
    ip = current_remote_ip()
    try:
        provider = _provider(ctx, provider_id)
    except MissingError:
        log.warning("saml login refused", provider_id=provider_id, reason="unknown_provider", ip=ip)
        raise
    try:
        user, assertion = service.consume_response(
            ctx.system_env(), provider, saml_response, relay_state, request.cookies.get(BINDING_COOKIE)
        )
    except saml.SamlError as exc:
        log.warning("saml login refused", provider_id=provider_id, reason=exc.code, ip=ip)
        raise AccessDenied(exc.message) from exc
    except AccessDenied as exc:
        log.warning("saml login refused", provider_id=provider_id, reason=exc.context.get("reason", "account"), ip=ip)
        raise
    # same session, cookie and CSRF token as a password login; like OIDC, the identity provider is trusted for
    # the authentication strength (no second local factor is asked)
    response = RedirectResponse("/", status_code=303)
    _finish_login(ctx, response, user, totp_verified=True)
    response.delete_cookie(BINDING_COOKIE, path=COOKIE_PATH)
    log.info(
        "saml login succeeded", provider_id=provider_id, user_id=user.ids[0], assertion=assertion.assertion_id, ip=ip
    )
    return response


@router.get("/{provider_id}/metadata")
def saml_metadata(provider_id: int, ctx: RequestContext = Depends(request_context)) -> Response:
    """SP metadata for the identity provider (administrators also get it for inactive providers)."""
    provider = _provider(ctx, provider_id, include_inactive=_is_admin(ctx))
    cert = provider.signing_certificate() if provider.sign_requests else None
    xml = saml.sp_metadata(
        sp_entity_id=provider.entity_id(),
        acs_url=provider.acs(),
        name_id_format=saml.NAMEID_FORMATS.get(str(provider.name_id_format or "")),
        want_assertions_signed=bool(provider.want_assertions_signed),
        signing_certificate=cert,
    )
    return Response(xml, media_type="application/xml")
