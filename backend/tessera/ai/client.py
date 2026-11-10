"""Client of the AI gateway.

Two transports:

* ``http`` (default, ``TESSERA_AI_GATEWAY_URL=http://gateway:8100``): the separate gateway service;
* ``inprocess`` (``TESSERA_AI_GATEWAY_URL=inprocess``): the gateway library runs inside the API
  process, reading its configuration through the ORM and writing its ledger through the caller's
  cursor (D-049). Used by tests, single-process deployments and the end-to-end suite; needs the
  ``tessera-gateway`` package installed.

The runtime only ever sees plain dictionaries in the gateway's envelope format.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import httpx

from tessera.ai.errors import AIError, AIUnavailable, from_code
from tessera.config import get_settings
from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment

T = TypeVar("T")


@dataclass
class CallContext:
    company_id: int
    user_id: int | None
    feature: str

    def headers(self) -> dict[str, str]:
        h = {"x-tessera-company": str(self.company_id), "x-tessera-feature": self.feature}
        if self.user_id is not None:
            h["x-tessera-user"] = str(self.user_id)
        return h


class GatewayClient(Protocol):
    def complete(self, ctx: CallContext, body: dict[str, Any]) -> dict[str, Any]: ...

    def stream(self, ctx: CallContext, body: dict[str, Any]) -> Iterator[dict[str, Any]]: ...

    def embed(self, ctx: CallContext, texts: list[str], input_type: str = "document") -> dict[str, Any]: ...

    def moderate(self, text: str, context: str = "") -> dict[str, Any]: ...

    def usage(self, params: dict[str, Any]) -> list[dict[str, Any]]: ...

    def health(self) -> dict[str, Any]: ...


# ----------------------------------------------------------------------------- http


class HttpGatewayClient:
    def __init__(self, url: str, token: str, *, timeout: float = 120.0, transport: httpx.BaseTransport | None = None):
        self.url = url.rstrip("/")
        self.client = httpx.Client(
            base_url=self.url,
            timeout=httpx.Timeout(timeout, connect=3.0),
            headers={"authorization": f"Bearer {token}"},
            transport=transport,
        )

    @staticmethod
    def _raise(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        try:
            err = resp.json().get("error", {})
        except ValueError:
            err = {}
        code = err.get("code") if isinstance(err, dict) else None
        raise from_code(
            code or "ai_unavailable", (err.get("message") if isinstance(err, dict) else None) or resp.text[:200]
        )

    def _post(self, path: str, ctx: CallContext | None, body: dict[str, Any]) -> Any:
        try:
            resp = self.client.post(path, json=body, headers=ctx.headers() if ctx else None)
        except httpx.HTTPError as exc:
            raise AIUnavailable(_("AI gateway unreachable: {exc}", exc=exc)) from exc
        self._raise(resp)
        return resp.json()

    def complete(self, ctx: CallContext, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self._post("/v1/complete", ctx, body)
        return result

    def stream(self, ctx: CallContext, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        try:
            with self.client.stream("POST", "/v1/stream", json=body, headers=ctx.headers()) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    self._raise(resp)
                event = ""
                for line in resp.iter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:])
                        data["event"] = data.get("event", event)
                        yield data
        except httpx.HTTPError as exc:
            raise AIUnavailable(_("AI gateway unreachable: {exc}", exc=exc)) from exc

    def embed(self, ctx: CallContext, texts: list[str], input_type: str = "document") -> dict[str, Any]:
        result: dict[str, Any] = self._post("/v1/embed", ctx, {"texts": texts, "input_type": input_type})
        return result

    def moderate(self, text: str, context: str = "") -> dict[str, Any]:
        result: dict[str, Any] = self._post("/v1/moderate", None, {"text": text, "context": context})
        return result

    def usage(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            resp = self.client.get("/v1/usage", params=params)
        except httpx.HTTPError as exc:
            raise AIUnavailable(_("AI gateway unreachable: {exc}", exc=exc)) from exc
        self._raise(resp)
        rows: list[dict[str, Any]] = resp.json()
        return rows

    def health(self) -> dict[str, Any]:
        try:
            resp = self.client.get("/v1/health", timeout=3.0)
        except httpx.HTTPError as exc:
            raise AIUnavailable(_("AI gateway unreachable: {exc}", exc=exc)) from exc
        self._raise(resp)
        data: dict[str, Any] = resp.json()
        return data


# ----------------------------------------------------------------------------- in-process


def run_sync(coro_factory: Callable[[], Any]) -> Any:
    """Run a coroutine from synchronous ORM code (in a worker thread when a loop is already running)."""
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False
    if not in_loop:
        return asyncio.run(coro_factory())
    with ThreadPoolExecutor(1) as pool:
        return pool.submit(lambda: asyncio.run(coro_factory())).result()


def iterate_sync(agen: AsyncIterator[T]) -> Iterator[T]:
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                yield loop.run_until_complete(agen.__anext__())
            except StopAsyncIteration:
                break
    finally:
        loop.run_until_complete(agen.aclose())  # type: ignore[attr-defined]
        loop.close()


_shared: dict[str, Any] = {}
_shared_lock = threading.Lock()


def _shared_state(dbname: str) -> tuple[Any, Any]:
    from gateway.breaker import CircuitBreaker
    from gateway.cache import MemoryCache

    with _shared_lock:
        if dbname not in _shared:
            _shared[dbname] = (CircuitBreaker(), MemoryCache())
        breaker, cache = _shared[dbname]
    return breaker, cache


def reset_inprocess_state() -> None:
    with _shared_lock:
        _shared.clear()


class OrmConfigStore:
    """Gateway configuration read through the ORM (so uncommitted test data and the caller's
    transaction are visible)."""

    def __init__(self, env: Environment) -> None:
        self.env = env

    def get(self) -> Any:
        from gateway.config import Budget, CompanySettings, GatewayConfig, ProviderConfig, Route, Target

        from tessera.security.crypto import decrypt

        env = self.env.elevated("ai gateway configuration")
        cfg = GatewayConfig()
        if "ai_core.provider" not in env.registry.models:
            return cfg
        for p in env["ai_core.provider"].search([("active", "=", True)]):
            cfg.providers[p.ids[0]] = ProviderConfig(
                id=p.ids[0],
                name=p.name,
                kind=p.kind,
                api_key=decrypt(p.api_key) if p.api_key else None,
                base_url=p.base_url or None,
                region=p.region or None,
                approved_for_pii=bool(p.approved_for_pii),
                retention=p.retention or "",
                options=dict(p.options or {}),
            )
        for r in env["ai_core.route"].search([("active", "=", True)], order="id"):
            fallback = (
                Target(r.fallback_provider_id.ids[0], r.fallback_model or "", dict(r.fallback_params or {}))
                if r.fallback_provider_id
                else None
            )
            cfg.routes.append(
                Route(
                    r.company_id.ids[0] if r.company_id else None,
                    r.task_class,
                    Target(r.provider_id.ids[0], r.model, dict(r.params or {})),
                    fallback,
                )
            )
        # company-specific routes first, as the SQL store orders them
        cfg.routes.sort(key=lambda r: r.company_id is None)
        for b in env["ai_core.budget"].search([("active", "=", True)]):
            cfg.budgets.append(
                Budget(
                    id=b.ids[0],
                    company_id=b.company_id.ids[0],
                    period=b.period,
                    user_id=b.user_id.ids[0] if b.user_id else None,
                    feature=b.feature or None,
                    limit_cost=Decimal(str(b.limit_cost)) if b.limit_cost else None,
                    limit_tokens=b.limit_tokens or None,
                    limit_requests=b.limit_requests or None,
                    hard=bool(b.hard),
                    alert_pct=b.alert_pct or 80,
                )
            )
        for f in env["ai_core.feature_flag"].search([]):
            cfg.flags.setdefault(f.feature_id.name, {})[f.company_id.ids[0] if f.company_id else None] = bool(f.enabled)
        for s in env["ai_core.company_setting"].search([]):
            if not s.company_id:
                cfg.global_enabled = bool(s.ai_enabled)
                continue
            cfg.companies[s.company_id.ids[0]] = CompanySettings(
                s.company_id.ids[0], bool(s.ai_enabled), list(s.allowed_regions or [])
            )
        return cfg


class CursorLedger:
    """The gateway ledger written through the caller's cursor (``ai_gateway.ledger``)."""

    def __init__(self, env: Environment) -> None:
        self.cr = env.cr

    async def record(self, entry: Any) -> None:
        from dataclasses import asdict

        from gateway.ledger import COLUMNS

        values = asdict(entry)
        self.cr.execute(
            f"INSERT INTO ai_gateway.ledger ({', '.join(COLUMNS)}) VALUES ({', '.join(':' + c for c in COLUMNS)})",  # noqa: S608
            values,
        )

    async def totals(
        self, company_id: int, since: dt.datetime, *, user_id: int | None = None, feature: str | None = None
    ) -> Any:
        from gateway.ledger import BILLABLE, Totals

        where = ["company_id = :c", "ts >= :since", "status = ANY(:billable)"]
        params: dict[str, Any] = {"c": company_id, "since": since, "billable": list(BILLABLE)}
        if user_id is not None:
            where.append("user_id = :u")
            params["u"] = user_id
        if feature is not None:
            where.append("feature = :f")
            params["f"] = feature
        row = self.cr.fetchall(
            "SELECT coalesce(sum(cost), 0), coalesce(sum(input_tokens + output_tokens + cache_read_tokens + "  # noqa: S608
            f"cache_write_tokens), 0), count(*) FROM ai_gateway.ledger WHERE {' AND '.join(where)}",
            params,
        )[0]
        return Totals(Decimal(row[0]), int(row[1]), int(row[2]))

    async def report(
        self, *, company_id: int | None, since: Any, until: Any, group_by: list[str]
    ) -> list[dict[str, Any]]:
        from gateway.ledger import COLUMNS, Entry, _aggregate

        where, params = ["true"], {}
        if company_id is not None:
            where.append("company_id = :c")
            params["c"] = company_id
        if since is not None:
            where.append("ts >= :since")
            params["since"] = since
        if until is not None:
            where.append("ts < :until")
            params["until"] = until
        rows = self.cr.fetchall(
            f"SELECT {', '.join(COLUMNS)} FROM ai_gateway.ledger WHERE {' AND '.join(where)}",  # noqa: S608
            params,
        )
        entries = [Entry(**dict(zip(COLUMNS, r, strict=True))) for r in rows]
        report: list[dict[str, Any]] = _aggregate(entries, group_by)
        return report


class InProcessGatewayClient:
    def __init__(self, env: Environment) -> None:
        self.env = env

    def _gateway(self) -> Any:
        from gateway.service import Gateway

        breaker, cache = _shared_state(self.env.registry.dbname)
        return Gateway(OrmConfigStore(self.env), CursorLedger(self.env), cache=cache, breaker=breaker)

    @staticmethod
    def _ctx(ctx: CallContext) -> Any:
        from gateway.service import CallContext as GwContext

        return GwContext(company_id=ctx.company_id, user_id=ctx.user_id, feature=ctx.feature)

    def _call(self, fn: Callable[[Any], Any]) -> Any:
        from gateway.errors import GatewayError

        gw = self._gateway()

        async def go() -> Any:
            try:
                return await fn(gw)
            finally:
                await gw.aclose()

        try:
            return run_sync(go)
        except GatewayError as exc:
            raise from_code(exc.code, exc.message) from exc

    def complete(self, ctx: CallContext, body: dict[str, Any]) -> dict[str, Any]:
        from gateway.envelope import CompleteRequest

        req = CompleteRequest.model_validate(body)
        resp = self._call(lambda gw: gw.complete(self._ctx(ctx), req))
        out: dict[str, Any] = json.loads(resp.model_dump_json())
        return out

    def stream(self, ctx: CallContext, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        from gateway.envelope import CompleteRequest

        req = CompleteRequest.model_validate(body)
        gw = self._gateway()
        for ev in iterate_sync(gw.stream(self._ctx(ctx), req)):
            yield {**json.loads(ev.model_dump_json(exclude_none=True)), "event": ev.event}

    def embed(self, ctx: CallContext, texts: list[str], input_type: str = "document") -> dict[str, Any]:
        from gateway.envelope import EmbedRequest

        req = EmbedRequest.model_validate({"texts": texts, "input_type": input_type})
        resp = self._call(lambda gw: gw.embed(self._ctx(ctx), req))
        out: dict[str, Any] = json.loads(resp.model_dump_json())
        return out

    def moderate(self, text: str, context: str = "") -> dict[str, Any]:
        from gateway.envelope import ModerateRequest
        from gateway.service import Gateway

        out: dict[str, Any] = Gateway.moderate(ModerateRequest(text=text, context=context)).model_dump()
        return out

    def usage(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        def parse(v: Any) -> Any:
            return dt.datetime.fromisoformat(v) if isinstance(v, str) else v

        rows: list[dict[str, Any]] = self._call(
            lambda gw: gw.usage(
                company_id=params.get("company_id"),
                since=parse(params.get("since")),
                until=parse(params.get("until")),
                group_by=list(params.get("group_by") or ["company_id", "feature"]),
            )
        )
        return rows

    def health(self) -> dict[str, Any]:
        out: dict[str, Any] = self._gateway().health()
        return out


# ----------------------------------------------------------------------------- factory

_override: Callable[[Environment], GatewayClient] | None = None


def set_client_factory(factory: Callable[[Environment], GatewayClient] | None) -> None:
    """Tests and tools can substitute the transport."""
    global _override
    _override = factory


def get_client(env: Environment) -> GatewayClient:
    if _override is not None:
        return _override(env)
    settings = get_settings()
    if settings.ai_gateway_url.strip().lower() == "inprocess":
        return InProcessGatewayClient(env)
    return HttpGatewayClient(settings.ai_gateway_url, settings.ai_gateway_token)


__all__ = [
    "AIError",
    "CallContext",
    "GatewayClient",
    "HttpGatewayClient",
    "InProcessGatewayClient",
    "get_client",
    "set_client_factory",
]
