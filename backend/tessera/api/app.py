"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from tessera import __version__
from tessera.api.errors import install_error_handlers
from tessera.api.middleware import RateLimitMiddleware, RequestContextMiddleware, SecurityHeadersMiddleware
from tessera.api.routers import attachments, auth, dataio, health, jsonrpc, metadata, realtime, records
from tessera.config import get_settings
from tessera.logging import configure_logging, get_logger

log = get_logger(__name__)

API_PREFIX = "/api/v1"


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    from tessera.realtime.hub import hub

    await hub.start_listener()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging()
    app = FastAPI(
        lifespan=_lifespan,
        title="Tessera API",
        version=__version__,
        description="Auto-generated REST and JSON-RPC access to every installed model, under the caller's permissions.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["ETag", "X-Request-Id", "Server-Timing"],
    )
    install_error_handlers(app)

    app.include_router(health.router)
    v1 = APIRouter(prefix=API_PREFIX)
    v1.include_router(auth.router)
    v1.include_router(records.router)
    v1.include_router(metadata.router)
    v1.include_router(attachments.router)
    v1.include_router(dataio.router)
    v1.include_router(jsonrpc.router)
    v1.include_router(realtime.router)
    _load_default_registry()
    for extra in _module_routers():
        v1.include_router(extra)
    app.include_router(v1)
    _mounted_apps.append(app)
    return app


_mounted_apps: list[FastAPI] = []


def _load_default_registry() -> None:
    """Import installed modules (their code registers API routers) before the routes are built."""
    from tessera.orm.registry import Registry

    try:
        Registry.get(get_settings().database_url)
    except Exception:
        log.warning("registry not loaded at startup; module API routes will mount on first registry build")


def mount_new_routers() -> None:
    """Mount routers registered after app creation (module installed into a running process)."""
    from tessera.api.registry import contributed_routers

    for app in _mounted_apps:
        known = {getattr(r, "path", None) for r in app.routes}
        for router in contributed_routers():
            if not any(f"{API_PREFIX}{getattr(r, 'path', '')}" in known for r in router.routes):
                app.include_router(router, prefix=API_PREFIX)
                app.openapi_schema = None


def _module_routers() -> list[APIRouter]:
    """Routers contributed by installed platform modules (web, chatter, ...)."""
    from tessera.api.registry import contributed_routers

    return contributed_routers()
