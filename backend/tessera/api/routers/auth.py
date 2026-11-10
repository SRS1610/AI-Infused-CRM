"""Login, logout, session info, TOTP, password change, API keys and OIDC."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from tessera.api.deps import RequestContext, admin_only, authenticated, authenticated_any, request_context
from tessera.config import get_settings
from tessera.exceptions import AccessDenied, MissingError, UserError
from tessera.i18n import _
from tessera.runtime import current_remote_ip, user_agent_var
from tessera.security import auth as auth_lib
from tessera.security.crypto import new_token

router = APIRouter(prefix="/auth", tags=["auth"])

_OIDC_STATE_COOKIE = "tessera_oidc"


class LoginRequest(BaseModel):
    login: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


class TotpRequest(BaseModel):
    pre_auth_token: str
    code: str = Field(min_length=6, max_length=20, description="TOTP code or a recovery code")


class TotpEnableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=10)


class PasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=512)


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    scopes: list[str] = Field(default_factory=list)
    expires_at: str | None = None


def _set_session_cookie(response: Response, token: str) -> None:
    settings = get_settings()
    response.set_cookie(
        settings.session_cookie_name,
        token,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=settings.session_absolute_hours * 3600,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(get_settings().session_cookie_name, path="/")


def session_payload(ctx: RequestContext) -> dict[str, Any]:
    env = ctx.env
    user = env.user.elevated()
    companies = user.company_ids or user.company_id
    groups = ctx.registry.security.user_groups(env) if hasattr(ctx.registry.security, "user_groups") else frozenset()
    group_records = env["core.group"].elevated().browse(sorted(groups)) if groups else env["core.group"].browse([])
    xmlids = group_records.get_external_id() if group_records else {}
    return {
        "user": {
            "id": user.ids[0],
            "login": user.login,
            "name": user.name,
            "email": user.email,
            "lang": user.lang,
            "tz": user.tz,
            "share": bool(user.share),
            "totp_enabled": bool(user.totp_enabled),
            "is_admin": ctx.registry.security.has_groups(env, ["core.group_system"]),
        },
        "company": {"id": env.company.ids[0], "name": env.company.elevated().name} if env.company else None,
        "allowed_companies": [{"id": c.ids[0], "name": c.name} for c in companies.elevated()],
        "selected_companies": env.companies.ids,
        "groups": sorted(x for x in xmlids.values() if x),
        "auth": ctx.auth_kind,
        "csrf_token": ctx.session.csrf_token if ctx.session is not None else None,
        "lang": env.lang,
        "modules": list(ctx.registry.installed_versions),
    }


def _finish_login(ctx: RequestContext, response: Response, user: Any, *, totp_verified: bool) -> dict[str, Any]:
    info = auth_lib.create_session(
        ctx.system_env(), user, ip=current_remote_ip(), user_agent=user_agent_var.get(), totp_verified=totp_verified
    )
    _set_session_cookie(response, info.token)
    ctx.env = ctx.env.__class__(ctx.env.transaction, user.ids[0], {})
    ctx.session, ctx.auth_kind, ctx.session_token = info.session, "session", info.token
    payload = session_payload(ctx)
    payload["csrf_token"] = info.csrf_token
    return payload


@router.post("/login")
def login(body: LoginRequest, response: Response, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    sys_env = ctx.system_env()
    user = auth_lib.login_with_password(sys_env, body.login, body.password)
    if user.elevated().totp_enabled:
        return {"totp_required": True, "pre_auth_token": auth_lib.issue_pre_auth(sys_env, user)}
    return _finish_login(ctx, response, user, totp_verified=False)


@router.post("/totp")
def login_totp(body: TotpRequest, response: Response, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    sys_env = ctx.system_env()
    user = auth_lib.consume_pre_auth(sys_env, body.pre_auth_token)
    code = body.code.strip()
    if not auth_lib.totp_verify(user.elevated().totp_secret, code) and not user.consume_recovery_code(code):
        raise AccessDenied(_("Invalid authentication code"))
    return _finish_login(ctx, response, user, totp_verified=True)


@router.post("/logout")
def logout(response: Response, ctx: RequestContext = Depends(request_context)) -> dict[str, Any]:
    if ctx.session_token:
        auth_lib.revoke_session(ctx.system_env(), ctx.session_token)
    _clear_session_cookie(response)
    return {"ok": True}


@router.get("/session")
def session_info(ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    return session_payload(ctx)


@router.post("/password")
def change_password(body: PasswordChangeRequest, ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    user = ctx.env.user
    if not user.check_password(body.current_password):
        raise AccessDenied(_("Current password is incorrect"))
    user.set_password(body.new_password)
    # other sessions of this user are revoked
    sys_env = ctx.system_env()
    others = sys_env["core.session"].search([("user_id", "=", user.ids[0]), ("revoked", "=", False)])
    if ctx.session is not None:
        others = others - ctx.session
    others.write({"revoked": True})
    return {"ok": True}


# -- TOTP enrolment -------------------------------------------------------------------------------


@router.post("/totp/setup")
def totp_setup(ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    user = ctx.env.user.elevated()
    secret = auth_lib.totp_new_secret()
    user.with_context(_recompute_write=True)._write_columns({"totp_secret": secret, "totp_enabled": False})
    return {"secret": secret, "uri": auth_lib.totp_provisioning_uri(secret, user.login)}


@router.post("/totp/enable")
def totp_enable(body: TotpEnableRequest, ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    user = ctx.env.user.elevated()
    if not auth_lib.totp_verify(user.totp_secret, body.code):
        raise UserError(_("The code does not match; scan the QR code again and retry"))
    user.write({"totp_enabled": True})
    return {"ok": True, "recovery_codes": user.generate_recovery_codes()}


@router.post("/totp/disable")
def totp_disable(body: TotpEnableRequest, ctx: RequestContext = Depends(authenticated_any)) -> dict[str, Any]:
    user = ctx.env.user.elevated()
    if not auth_lib.totp_verify(user.totp_secret, body.code):
        raise AccessDenied(_("Invalid authentication code"))
    user.with_context(_recompute_write=True)._write_columns({"totp_secret": None, "totp_enabled": False})
    return {"ok": True}


# -- API keys -------------------------------------------------------------------------------------


@router.get("/api-keys")
def list_api_keys(ctx: RequestContext = Depends(authenticated)) -> list[dict[str, Any]]:
    return ctx.env["core.api_key"].search_read(
        [], ["name", "key_prefix", "scopes", "expires_at", "last_used_at", "active", "user_id"]
    )


@router.post("/api-keys", status_code=201)
def create_api_key(body: ApiKeyCreate, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    plain, prefix, digest = auth_lib.generate_api_key()
    key = ctx.env["core.api_key"].create(
        {"name": body.name, "scopes": body.scopes, "expires_at": body.expires_at, "user_id": ctx.env.uid}
    )
    key.elevated().with_context(_recompute_write=True)._write_columns({"key_prefix": prefix, "key_hash": digest})
    return {"id": key.ids[0], "name": key.name, "key": plain, "prefix": prefix}


@router.delete("/api-keys/{key_id}")
def revoke_api_key(key_id: int, ctx: RequestContext = Depends(authenticated)) -> dict[str, Any]:
    key = ctx.env["core.api_key"].browse([key_id]).exists()
    if not key:
        raise MissingError(_("API key not found"))
    key.write({"active": False})
    return {"ok": True}


# -- OIDC -----------------------------------------------------------------------------------------


def _provider(ctx: RequestContext, code: str) -> Any:
    provider = ctx.system_env()["core.sso_provider"].search([("code", "=", code), ("enabled", "=", True)], limit=1)
    if not provider:
        raise MissingError(_("Unknown identity provider '{code}'", code=code))
    return provider


@router.get("/providers")
def list_providers(ctx: RequestContext = Depends(request_context)) -> list[dict[str, Any]]:
    providers = ctx.system_env()["core.sso_provider"].search([("enabled", "=", True)])
    return [{"code": p.code, "name": p.button_label or p.name} for p in providers]


@router.get("/oidc/{code}/start")
def oidc_start(code: str, request: Request, ctx: RequestContext = Depends(request_context)) -> Response:
    provider = _provider(ctx, code)
    client = auth_lib.OidcClient(provider)
    state, nonce = new_token(16), new_token(16)
    verifier, challenge = client.pkce_pair()
    redirect_uri = str(request.url_for("oidc_callback", code=code))
    url = client.authorization_url(redirect_uri, state, nonce, challenge)
    response = RedirectResponse(url, status_code=302)
    settings = get_settings()
    response.set_cookie(
        _OIDC_STATE_COOKIE,
        f"{state}.{nonce}.{verifier}",
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        max_age=600,
        path="/api/v1/auth/oidc/",
    )
    return response


@router.get("/oidc/{code}/callback", name="oidc_callback")
def oidc_callback(code: str, request: Request, ctx: RequestContext = Depends(request_context)) -> Response:
    provider = _provider(ctx, code)
    cookie = request.cookies.get(_OIDC_STATE_COOKIE, "")
    try:
        state, nonce, verifier = cookie.split(".", 2)
    except ValueError as exc:
        raise AccessDenied(_("Login session expired; start again")) from exc
    if not secrets.compare_digest(state, request.query_params.get("state", "")):
        raise AccessDenied(_("State mismatch"))
    auth_code = request.query_params.get("code")
    if not auth_code:
        raise AccessDenied(request.query_params.get("error_description") or "Identity provider returned no code")
    client = auth_lib.OidcClient(provider)
    tokens = client.exchange_code(auth_code, str(request.url_for("oidc_callback", code=code)), verifier, nonce)
    user = auth_lib.user_for_oidc_claims(ctx.system_env(), provider, tokens.claims)
    response = RedirectResponse("/", status_code=302)
    _finish_login(ctx, response, user, totp_verified=True)
    response.delete_cookie(_OIDC_STATE_COOKIE, path="/api/v1/auth/oidc/")
    return response


@router.get("/sessions")
def list_sessions(ctx: RequestContext = Depends(admin_only)) -> list[dict[str, Any]]:
    return ctx.env["core.session"].search_read(
        [("revoked", "=", False), ("pre_auth", "=", False)],
        ["user_id", "ip", "user_agent", "last_seen", "expires_at", "totp_verified"],
        limit=200,
    )
