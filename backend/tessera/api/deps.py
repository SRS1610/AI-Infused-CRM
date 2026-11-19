"""Request-scoped database transaction and authenticated environment.

Every API handler receives a :class:`RequestContext` whose ``env`` carries the caller's
permissions. The transaction commits when the handler returns normally and rolls back on any
exception. Authentication order: API key (``Authorization: Bearer tsk_…``) → session cookie.
Cookie-authenticated unsafe requests must carry the session's CSRF token in ``X-CSRF-Token``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Request

from tessera.config import get_settings
from tessera.db.engine import Cursor, get_engine
from tessera.exceptions import AccessDenied, AccessError, UserError
from tessera.i18n import _, set_request_language
from tessera.logging import bind_context
from tessera.orm.environment import SYSTEM_UID, Environment
from tessera.orm.model import Model
from tessera.orm.registry import Registry, current_registry
from tessera.security.auth import API_KEY_PREFIX, resolve_api_key, resolve_session
from tessera.security.crypto import constant_time_equals

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ANONYMOUS_UID = 0
CSRF_EXEMPT_ANONYMOUS = {"/api/v1/auth/login", "/api/v1/auth/totp", "/api/v1/auth/logout"}
# login endpoints contributed by modules (e.g. a SAML assertion consumer, posted by the identity provider's page)
_CSRF_EXEMPT_ANONYMOUS_PATTERNS: list[re.Pattern[str]] = []


def exempt_login_path(pattern: str) -> None:
    """Treat unsafe requests to paths fully matching ``pattern`` that carry a session cookie but no valid CSRF
    token as anonymous (instead of refusing them), like ``/auth/login``. Only for endpoints that authenticate
    the caller themselves and never act on an existing session."""
    compiled = re.compile(pattern)
    if all(p.pattern != compiled.pattern for p in _CSRF_EXEMPT_ANONYMOUS_PATTERNS):
        _CSRF_EXEMPT_ANONYMOUS_PATTERNS.append(compiled)


def _csrf_exempt(path: str) -> bool:
    return path in CSRF_EXEMPT_ANONYMOUS or any(p.fullmatch(path) for p in _CSRF_EXEMPT_ANONYMOUS_PATTERNS)


@dataclass
class RequestContext:
    request: Request
    cr: Cursor
    registry: Registry
    env: Environment
    auth_kind: str = "anonymous"  # anonymous | session | api_key
    session: Model | None = None
    api_key: Model | None = None
    session_token: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def uid(self) -> int:
        return self.env.uid

    @property
    def authenticated(self) -> bool:
        return self.auth_kind != "anonymous"

    def system_env(self) -> Environment:
        return Environment.new(self.registry, self.cr, SYSTEM_UID, su=True) if self.env.uid != SYSTEM_UID else self.env


def database_url_for(request: Request) -> str:
    """Single-database default; the database-per-tenant router (host → DSN) plugs in here."""
    return get_settings().database_url


def header_language(request: Request) -> str | None:
    """``X-Tessera-Lang``, else the first ``Accept-Language`` entry (``fr-FR`` → ``fr_FR``)."""
    lang = request.headers.get("x-tessera-lang")
    if not lang:
        accept = request.headers.get("accept-language", "")
        lang = accept.split(",")[0].split(";")[0].strip()
    lang = lang.replace("-", "_")[:16]
    return lang if lang and lang != "*" else None


def request_language(request: Request, env: Environment | None = None) -> str | None:
    """The caller's language: the authenticated user's (or the header override), else the headers."""
    if env is not None and env.context.get("lang"):
        return str(env.context["lang"])
    return header_language(request)


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


def _user_context(sys_env: Environment, request: Request, user: Model) -> dict[str, Any]:
    user = user.elevated()
    allowed = list(user.company_ids.ids) or list(user.company_id.ids)
    chosen = request.headers.get("x-tessera-companies")
    if chosen:
        try:
            requested = [int(c) for c in chosen.split(",") if c.strip()]
        except ValueError as exc:
            raise UserError(_("Invalid X-Tessera-Companies header")) from exc
        selected = [c for c in requested if c in allowed]
        if not selected:
            raise AccessError(_("None of the requested companies is allowed for this user"))
        allowed = selected
    current_header = request.headers.get("x-tessera-company")
    current = int(current_header) if current_header and current_header.isdigit() else None
    if current not in allowed:
        current = user.company_id.ids[0] if user.company_id.ids and user.company_id.ids[0] in allowed else allowed[0]
    ctx: dict[str, Any] = {"allowed_company_ids": allowed, "company_id": current}
    lang = request.headers.get("x-tessera-lang") or user.lang
    if lang:
        ctx["lang"] = lang
    tz = request.headers.get("x-tessera-tz") or user.tz
    if tz:
        ctx["tz"] = tz
    return ctx


def _authenticate(ctx: RequestContext) -> None:
    request = ctx.request
    sys_env = ctx.system_env()
    bearer = _bearer_token(request)
    if bearer and bearer.startswith(API_KEY_PREFIX):
        key = resolve_api_key(sys_env, bearer)
        if key is None:
            raise AccessDenied(_("Invalid or expired API key"))
        user = key.user_id
        ctx.env = Environment(ctx.env.transaction, user.ids[0], _user_context(sys_env, request, user))
        ctx.auth_kind, ctx.api_key = "api_key", key
        return
    token = request.cookies.get(get_settings().session_cookie_name)
    if token:
        session = resolve_session(sys_env, token)
        if session is not None and request.method not in SAFE_METHODS:
            csrf = request.headers.get("x-csrf-token")
            if not csrf or not constant_time_equals(csrf, str(session.csrf_token)):
                if not _csrf_exempt(request.url.path):
                    raise AccessError(_("Missing or invalid CSRF token"), csrf=True)
                # logging in again with a stale cookie: proceed as anonymous instead of failing
                token, session = None, None
        if session is not None:
            user = session.user_id
            ctx.env = Environment(ctx.env.transaction, user.ids[0], _user_context(sys_env, request, user))
            ctx.auth_kind, ctx.session, ctx.session_token = "session", session, token
            return
    public = sys_env["core.user"].with_context(active_test=False).search([("login", "=", "public")], limit=1)
    # never fall back to the system user: without a public user, anonymous callers get uid 0 (no rights)
    ctx.env = Environment(ctx.env.transaction, public.ids[0] if public else ANONYMOUS_UID, {})
    ctx.auth_kind = "anonymous"


def _open_context(request: Request) -> Iterator[RequestContext]:
    url = database_url_for(request)
    cr = Cursor(get_engine(url))
    registry = current_registry(url, cr)
    try:
        env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        ctx = RequestContext(request=request, cr=cr, registry=registry, env=env)
        _authenticate(ctx)
        set_request_language(request_language(request, ctx.env), registry.module_order)
        bind_context(uid=ctx.env.uid, db=registry.dbname, auth=ctx.auth_kind)
        request.state.ctx = ctx
        yield ctx
        ctx.env.flush()
        cr.commit()
    except BaseException:
        cr.rollback()
        raise
    finally:
        cr.close()


def request_context(request: Request) -> Iterator[RequestContext]:
    """Dependency: transaction + environment; anonymous callers get the public user."""
    yield from _open_context(request)


def authenticated(ctx: RequestContext = Depends(request_context)) -> RequestContext:
    if not ctx.authenticated:
        raise AccessDenied(_("Authentication required"))
    user = ctx.env.user.elevated()
    if user.share:
        raise AccessError(_("Portal users cannot use the back-office API"))
    return ctx


def authenticated_any(ctx: RequestContext = Depends(request_context)) -> RequestContext:
    """Authenticated internal *or* portal user."""
    if not ctx.authenticated:
        raise AccessDenied(_("Authentication required"))
    return ctx


def admin_only(ctx: RequestContext = Depends(authenticated)) -> RequestContext:
    if not ctx.env.registry.security.has_groups(ctx.env, ["core.group_system"]):
        raise AccessError(_("Administrator rights required"))
    return ctx
