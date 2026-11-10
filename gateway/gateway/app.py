"""HTTP API of the gateway (internal: service token + ``X-Tessera-Company`` / ``-User`` / ``-Feature``)."""

import datetime as dt
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import generate_latest

from gateway import metrics
from gateway.breaker import CircuitBreaker
from gateway.cache import MemoryCache, RedisCache, ResponseCache
from gateway.config import (
    ConfigStore,
    GatewayConfig,
    ProviderConfig,
    SqlConfigStore,
    StaticConfigStore,
    default_routes_for,
)
from gateway.envelope import (
    CompleteRequest,
    CompleteResponse,
    EmbedRequest,
    EmbedResponse,
    ModerateRequest,
    ModerateResponse,
)
from gateway.errors import GatewayError, NotSupported, Unauthorized
from gateway.ledger import GROUP_KEYS, Ledger, MemoryLedger, SqlLedger
from gateway.service import CallContext, Gateway
from gateway.settings import Settings, get_settings


def static_config(settings: Settings) -> GatewayConfig:
    """Single-provider configuration from the environment (no database)."""
    kind = settings.static_provider
    options: dict[str, Any] = {}
    base_url = None
    if kind == "openai_compatible":
        base_url = (settings.ollama_base_url or "http://localhost:11434") + "/v1"
        options = {"model": "llama3.1", "model_embed": "nomic-embed-text", "json_mode": "object"}
    provider = ProviderConfig(
        id=1,
        name=kind,
        kind=kind,
        api_key=settings.env_keys().get(kind),
        base_url=base_url,
        region="self-hosted" if kind in ("mock", "openai_compatible") else None,
        approved_for_pii=kind in ("mock", "openai_compatible"),
        options=options,
    )
    return GatewayConfig(providers={1: provider}, routes=default_routes_for(provider))


def build_gateway(settings: Settings) -> Gateway:
    store: ConfigStore
    ledger: Ledger
    if settings.database_url:
        store = SqlConfigStore(
            settings.database_url,
            master_key=settings.master_key,
            ttl=settings.config_ttl_seconds,
            env_keys=settings.env_keys(),
        )
        ledger = SqlLedger(settings.database_url)
    else:
        store = StaticConfigStore(static_config(settings))
        ledger = MemoryLedger()
    cache: ResponseCache = RedisCache(settings.redis_url) if settings.redis_url else MemoryCache()
    return Gateway(
        store,
        ledger,
        cache=cache,
        breaker=CircuitBreaker(settings.breaker_threshold, settings.breaker_cooldown_seconds),
    )


def create_app(gateway: Gateway | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.check()
    gw = gateway or build_gateway(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await gw.aclose()

    app = FastAPI(title="Tessera AI gateway", version="0.1.0", lifespan=lifespan)
    app.state.gateway = gw

    @app.exception_handler(GatewayError)
    async def gateway_error(_: Request, exc: GatewayError) -> JSONResponse:
        return JSONResponse({"error": exc.to_dict()}, status_code=exc.status)

    def auth(authorization: Annotated[str, Header()] = "") -> None:
        token = authorization.removeprefix("Bearer ").strip()
        if not token or not hmac.compare_digest(token.encode(), settings.service_token.encode()):
            raise Unauthorized("invalid service token")

    def context(
        x_tessera_company: Annotated[int, Header()],
        x_tessera_user: Annotated[int | None, Header()] = None,
        x_tessera_feature: Annotated[str, Header()] = "",
    ) -> CallContext:
        return CallContext(company_id=x_tessera_company, user_id=x_tessera_user, feature=x_tessera_feature)

    authed = Depends(auth)

    @app.post("/v1/complete", dependencies=[authed])
    async def complete(req: CompleteRequest, ctx: Annotated[CallContext, Depends(context)]) -> CompleteResponse:
        ctx.feature = ctx.feature or req.metadata.feature
        return await gw.complete(ctx, req)

    @app.post("/v1/stream", dependencies=[authed])
    async def stream(req: CompleteRequest, ctx: Annotated[CallContext, Depends(context)]) -> StreamingResponse:
        ctx.feature = ctx.feature or req.metadata.feature

        async def events() -> AsyncIterator[str]:
            async for ev in gw.stream(ctx, req):
                yield f"event: {ev.event}\ndata: {ev.model_dump_json(exclude_none=True)}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream", headers={"cache-control": "no-cache"})

    @app.post("/v1/embed", dependencies=[authed])
    async def embed(req: EmbedRequest, ctx: Annotated[CallContext, Depends(context)]) -> EmbedResponse:
        ctx.feature = ctx.feature or req.metadata.feature
        return await gw.embed(ctx, req)

    @app.post("/v1/moderate", dependencies=[authed])
    async def moderate(req: ModerateRequest) -> ModerateResponse:
        return gw.moderate(req)

    @app.post("/v1/transcribe", dependencies=[authed])
    async def transcribe() -> None:
        raise NotSupported("speech-to-text adapters arrive with the telephony integrations (Phase 4)")

    @app.post("/v1/ocr", dependencies=[authed])
    async def ocr() -> None:
        raise NotSupported("document OCR adapters arrive with expenses and documents (Phases 2 and 8)")

    @app.get("/v1/usage", dependencies=[authed])
    async def usage(
        company_id: int | None = None,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        group_by: Annotated[list[str], Query()] = ["company_id", "feature"],  # noqa: B006 - FastAPI query default
    ) -> list[dict[str, Any]]:
        keys = [g for g in group_by if g in GROUP_KEYS] or ["company_id"]
        return await gw.usage(company_id=company_id, since=since, until=until, group_by=keys)

    @app.get("/v1/health")
    async def health() -> dict[str, Any]:
        return gw.health()

    @app.get("/metrics")
    async def prometheus() -> Response:
        return Response(generate_latest(metrics.REGISTRY), media_type="text/plain; version=0.0.4")

    return app
