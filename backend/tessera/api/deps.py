"""Request-scoped database transaction and authenticated environment.

Every API handler receives a :class:`RequestContext` whose ``env`` carries the caller's
permissions. The transaction commits when the handler returns normally and rolls back on any
exception. Authentication order: API key (``Authorization: Bearer tsk_…``) → session cookie.
Cookie-authenticated unsafe requests must carry the session's CSRF token in ``X-CSRF-Token``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Request

from tessera.config import get_settings
from tessera.db.engine import Cursor, get_engine
from tessera.exceptions import AccessDenied, AccessError, UserError
from tessera.logging import bind_context
from tessera.orm.environment import SYSTEM_UID, Environment
from tessera.orm.model import Model
from tessera.orm.registry import Registry
from tessera.security.auth import API_KEY_PREFIX, resolve_api_key, resolve_session

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


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
            raise UserError("Invalid X-Tessera-Companies header") from exc
        selected = [c for c in requested if c in allowed]
        if not selected:
            raise AccessError("None of the requested companies is allowed for this user")
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
            raise AccessDenied("Invalid or expired API key")
        user = key.user_id
        ctx.env = Environment(ctx.env.transaction, user.ids[0], _user_context(sys_env, request, user))
        ctx.auth_kind, ctx.api_key = "api_key", key
        return
    token = request.cookies.get(get_settings().session_cookie_name)
    if token:
        session = resolve_session(sys_env, token)
        if session is not None:
            if request.method not in SAFE_METHODS:
                csrf = request.headers.get("x-csrf-token")
                if not csrf or csrf != session.csrf_token:
                    raise AccessError("Missing or invalid CSRF token", csrf=True)
            user = session.user_id
            ctx.env = Environment(ctx.env.transaction, user.ids[0], _user_context(sys_env, request, user))
            ctx.auth_kind, ctx.session, ctx.session_token = "session", session, token
            return
    public = sys_env["core.user"].with_context(active_test=False).search([("login", "=", "public")], limit=1)
    ctx.env = Environment(ctx.env.transaction, public.ids[0] if public else SYSTEM_UID, {})
    if not public:
        ctx.env = Environment(ctx.env.transaction, SYSTEM_UID, {}, su=False)
    ctx.auth_kind = "anonymous"


def _open_context(request: Request) -> Iterator[RequestContext]:
    url = database_url_for(request)
    registry = Registry.get(url)
    cr = Cursor(get_engine(url))
    try:
        env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        ctx = RequestContext(request=request, cr=cr, registry=registry, env=env)
        _authenticate(ctx)
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
        raise AccessDenied("Authentication required")
    user = ctx.env.user.elevated()
    if user.share:
        raise AccessError("Portal users cannot use the back-office API")
    return ctx


def authenticated_any(ctx: RequestContext = Depends(request_context)) -> RequestContext:
    """Authenticated internal *or* portal user."""
    if not ctx.authenticated:
        raise AccessDenied("Authentication required")
    return ctx


def admin_only(ctx: RequestContext = Depends(authenticated)) -> RequestContext:
    if not ctx.env.registry.security.has_groups(ctx.env, ["core.group_system"]):
        raise AccessError("Administrator rights required")
    return ctx
