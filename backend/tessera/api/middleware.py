"""Request id, security headers and rate limiting."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from tessera.config import get_settings
from tessera.logging import bind_context, clear_context, get_logger
from tessera.runtime import client_id_var, new_request_id, remote_ip_var, user_agent_var

log = get_logger(__name__)

Next = Callable[[Request], Awaitable[Response]]


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assign a request id, bind it to logs, record client ip / user agent, time the request."""

    async def dispatch(self, request: Request, call_next: Next) -> Response:
        rid = request.headers.get("x-request-id") or new_request_id()
        forwarded = request.headers.get("x-forwarded-for")
        ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else None)
        remote_ip_var.set(ip)
        user_agent_var.set(request.headers.get("user-agent"))
        client_id_var.set((request.headers.get("x-tessera-client") or "")[:64] or None)
        bind_context(request_id=rid, path=request.url.path, method=request.method)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            clear_context()
        response.headers["X-Request-Id"] = rid
        response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Next) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(self), geolocation=(self)")
        if get_settings().is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=63072000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class SlidingWindowLimiter:
    """In-process sliding-window limiter (per key). Redis-backed variant arrives with 1.3."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, limit: int, window: float = 60.0) -> bool:
        now = time.monotonic()
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            return True


limiter = SlidingWindowLimiter()

_LOGIN_PATHS = ("/api/v1/auth/login", "/api/v1/auth/totp", "/api/v1/auth/oidc/")


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Next) -> Response:
        settings = get_settings()
        ip = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            ip = forwarded.split(",")[0].strip()
        path = request.url.path
        if path.startswith(_LOGIN_PATHS) and request.method == "POST":
            if not limiter.allow(f"login:{ip}", settings.login_rate_limit_per_minute):
                return JSONResponse(status_code=429, content={"error": "RateLimited", "message": "Too many attempts"})
        elif path.startswith("/api/"):
            auth = request.headers.get("authorization", "")
            key = f"api:{auth[-16:]}" if auth else f"api:{ip}"
            if not limiter.allow(key, settings.api_rate_limit_per_minute):
                return JSONResponse(status_code=429, content={"error": "RateLimited", "message": "Rate limit exceeded"})
        return await call_next(request)
