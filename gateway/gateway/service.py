"""The gateway pipeline.

For each call: kill switches → routing and policy (residency, PII approval) → budgets and quotas →
redaction → response cache → provider call with circuit breaker and fallback → schema validation
with one repair → re-hydration → output screening → cost → ledger. The gateway never executes
tools; ``tool_use`` blocks go back to the runtime.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from gateway import metrics
from gateway.adapters import Adapter, ProviderRequest, ProviderResponse, build_adapter
from gateway.breaker import CircuitBreaker
from gateway.cache import ResponseCache, cache_key
from gateway.config import ConfigStore, GatewayConfig, ProviderConfig, Target
from gateway.envelope import (
    EMBEDDING_DIMENSIONS,
    Block,
    CompleteRequest,
    CompleteResponse,
    EmbedRequest,
    EmbedResponse,
    Message,
    ModerateRequest,
    ModerateResponse,
    Stop,
    StreamEvent,
    TaskClass,
    Usage,
)
from gateway.errors import (
    BudgetExceeded,
    FeatureDisabled,
    GatewayError,
    InvalidOutput,
    NoRoute,
    NotSupported,
    PolicyRefused,
    ProviderError,
    ProviderUnavailable,
    QuotaExceeded,
)
from gateway.ledger import Entry, Ledger, period_start
from gateway.pricing import cost_of
from gateway.redaction import Redactor, StreamRestorer
from gateway.safety import moderate, screen
from gateway.validation import SchemaMismatch, parse_and_validate, repair_message

EFFORT = {
    TaskClass.classify: "low",
    TaskClass.extract: "low",
    TaskClass.translation: "low",
    TaskClass.nl_query: "low",
    TaskClass.summarize: "medium",
    TaskClass.draft: "medium",
    TaskClass.vision: "medium",
    TaskClass.transcribe: "low",
    TaskClass.reason: "high",
}


@dataclass
class CallContext:
    company_id: int
    user_id: int | None = None
    feature: str = ""


@dataclass
class _Plan:
    targets: list[tuple[ProviderConfig, Target]]
    redactor: Redactor | None
    request: CompleteRequest  # redacted copy
    budget_warning: bool


class Gateway:
    def __init__(
        self,
        store: ConfigStore,
        ledger: Ledger,
        *,
        cache: ResponseCache | None = None,
        breaker: CircuitBreaker | None = None,
        http_client_factory: Callable[[ProviderConfig], Any] | None = None,
    ) -> None:
        self.store = store
        self.ledger = ledger
        self.cache = cache
        self.breaker = breaker or CircuitBreaker()
        self.http_client_factory = http_client_factory
        self._adapters: dict[tuple[Any, ...], Adapter] = {}

    # -- plumbing -------------------------------------------------------------------------
    def adapter(self, provider: ProviderConfig) -> Adapter:
        key = (
            provider.id,
            provider.kind,
            provider.api_key,
            provider.base_url,
            json.dumps(provider.options, sort_keys=True, default=str),
        )
        if key not in self._adapters:
            http = self.http_client_factory(provider) if self.http_client_factory else None
            self._adapters[key] = build_adapter(provider, http)
        return self._adapters[key]

    @staticmethod
    def _breaker_key(provider: ProviderConfig, model: str) -> str:
        return f"{provider.name}:{model}"

    def _targets(
        self, cfg: GatewayConfig, ctx: CallContext, task_class: str, needs_pii: bool
    ) -> list[tuple[ProviderConfig, Target]]:
        route = cfg.route_for(ctx.company_id, task_class)
        if route is None:
            raise NoRoute(f"no route for task class '{task_class}'", task_class=task_class)
        settings = cfg.companies.get(ctx.company_id)
        allowed_regions = set(settings.allowed_regions) if settings else set()
        out, refused = [], []
        for target in (route.primary, route.fallback):
            if target is None:
                continue
            provider = cfg.providers.get(target.provider_id)
            if provider is None or not provider.active:
                continue
            if allowed_regions and (provider.region or "") not in allowed_regions:
                refused.append(f"{provider.name}: region {provider.region or 'unknown'} not allowed")
                continue
            if needs_pii and not provider.approved_for_pii:
                refused.append(f"{provider.name}: not approved for personal data")
                continue
            out.append((provider, target))
        if not out:
            raise PolicyRefused("; ".join(refused) or "no usable provider", task_class=task_class)
        return out

    async def _check_budgets(self, cfg: GatewayConfig, ctx: CallContext, estimate: Decimal, tokens: int) -> bool:
        warning = False
        for b in cfg.budgets_for(ctx.company_id):
            if b.user_id is not None and b.user_id != ctx.user_id:
                continue
            if b.feature and b.feature != ctx.feature:
                continue
            totals = await self.ledger.totals(
                ctx.company_id, period_start(b.period), user_id=b.user_id, feature=b.feature or None
            )
            over = (
                (b.limit_cost is not None and totals.cost + estimate > Decimal(b.limit_cost))
                or (b.limit_tokens is not None and totals.tokens + tokens > b.limit_tokens)
                or (b.limit_requests is not None and totals.requests + 1 > b.limit_requests)
            )
            if over and b.hard:
                err = QuotaExceeded if b.user_id is not None else BudgetExceeded
                raise err(
                    f"{'user quota' if b.user_id is not None else 'company budget'} for this {b.period} is used up",
                    budget_id=b.id,
                    period=b.period,
                )
            if b.limit_cost is not None and totals.cost >= Decimal(b.limit_cost) * b.alert_pct / 100:
                warning = True
            if over:
                warning = True
        return warning

    @staticmethod
    def _redact_request(req: CompleteRequest, redactor: Redactor) -> CompleteRequest:
        def block(b: Block) -> Block:
            return b.model_copy(
                update={
                    "text": redactor.redact(b.text) if b.text else b.text,
                    "content": redactor.redact(b.content) if b.content else b.content,
                }
            )

        messages = [
            Message(
                role=m.role,
                content=redactor.redact(m.content) if isinstance(m.content, str) else [block(b) for b in m.content],
            )
            for m in req.messages
        ]
        return req.model_copy(update={"system": redactor.redact(req.system), "messages": messages})

    async def _plan(self, ctx: CallContext, req: CompleteRequest) -> _Plan:
        cfg = self.store.get()
        if (ctx.feature and not cfg.feature_enabled(ctx.feature, ctx.company_id)) or not cfg.feature_enabled(
            "*", ctx.company_id
        ):
            raise FeatureDisabled(f"AI feature '{ctx.feature}' is disabled", feature=ctx.feature)
        targets = self._targets(cfg, ctx, req.task_class.value, needs_pii=req.redaction_policy != "redact")
        provider, target = targets[0]
        text_len = len(req.system) + sum(len(b.text or b.content or "") for m in req.messages for b in m.blocks())
        est_usage = Usage(input_tokens=text_len // 4 + 1)
        estimate = cost_of(target.model, est_usage, provider.options.get("pricing"))
        warning = await self._check_budgets(cfg, ctx, estimate, est_usage.input_tokens)
        redactor = None
        if req.redaction_policy == "redact":
            redactor = Redactor(req.metadata.redaction_names)
            req = self._redact_request(req, redactor)
        return _Plan(targets, redactor, req, warning)

    def _provider_request(self, req: CompleteRequest, target: Target) -> ProviderRequest:
        params = dict(target.params)
        effort = params.pop("effort", None) or EFFORT.get(req.task_class)
        return ProviderRequest(
            model=target.model,
            system=req.system,
            messages=list(req.messages),
            tools=list(req.tools),
            output_schema=req.output_schema,
            max_output_tokens=int(params.pop("max_output_tokens", req.max_output_tokens)),
            effort=effort,
            cache=req.cache_hints,
            params=params,
            metadata=req.metadata.model_dump(),
        )

    async def _ledger(
        self,
        ctx: CallContext,
        req: CompleteRequest,
        kind: str,
        *,
        status: str,
        provider: ProviderConfig | None = None,
        model: str = "",
        usage: Usage | None = None,
        cost: Decimal = Decimal(0),
        latency_ms: int = 0,
        fallback: bool = False,
        cached: bool = False,
        redacted: bool = False,
        geo: str | None = None,
        error: str = "",
    ) -> None:
        usage = usage or Usage()
        entry = Entry(
            company_id=ctx.company_id,
            user_id=ctx.user_id,
            feature=ctx.feature or req.metadata.feature,
            prompt_version=req.metadata.prompt_version,
            run_id=req.metadata.run_id,
            task_class=req.task_class.value,
            kind=kind,
            provider=provider.name if provider else "",
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost=cost,
            latency_ms=latency_ms,
            status=status,
            fallback=fallback,
            cached=cached,
            redacted=redacted,
            inference_geo=geo,
            retention=provider.retention if provider else "",
            error=error[:500],
        )
        await self.ledger.record(entry)
        labels = {"feature": entry.feature, "provider": entry.provider, "model": model}
        metrics.REQUESTS.labels(status=status, **labels).inc()
        if usage.input_tokens or usage.output_tokens:
            metrics.TOKENS.labels(direction="input", **labels).inc(usage.input_tokens + usage.cache_read_tokens)
            metrics.TOKENS.labels(direction="output", **labels).inc(usage.output_tokens)
            metrics.COST.labels(**labels).inc(float(cost))
            metrics.LATENCY.labels(feature=entry.feature, provider=entry.provider).observe(latency_ms / 1000)
        if fallback:
            metrics.FALLBACKS.labels(feature=entry.feature).inc()

    def _context_text(self, req: CompleteRequest) -> str:
        return req.system + "\n" + "\n".join(b.text or b.content or "" for m in req.messages for b in m.blocks())

    # -- complete ---------------------------------------------------------------------------
    async def complete(self, ctx: CallContext, req: CompleteRequest) -> CompleteResponse:
        try:
            plan = await self._plan(ctx, req)
        except GatewayError as exc:
            await self._ledger(ctx, req, "complete", status=exc.code, error=exc.message)
            raise
        red = plan.redactor
        first_provider, first_target = plan.targets[0]
        key = None
        if req.metadata.cache_ttl > 0 and self.cache is not None:
            key = cache_key(
                ctx.company_id,
                req.metadata.prompt_version,
                first_target.model,
                {"req": plan.request.model_dump(exclude={"metadata"}), "extra": req.metadata.cache_key_extra},
            )
            hit = await self.cache.get(key)
            if hit:
                cached = CompleteResponse.model_validate_json(hit)
                cached = cached.model_copy(update={"cached": True, "cost": Decimal(0), "latency_ms": 0})
                await self._ledger(
                    ctx, req, "complete", status="ok", provider=first_provider, model=cached.model, cached=True
                )
                return cached

        started = time.monotonic()
        last_error: ProviderError | None = None
        for index, (provider, target) in enumerate(plan.targets):
            bkey = self._breaker_key(provider, target.model)
            if not self.breaker.allow(bkey):
                last_error = ProviderError(f"circuit open for {bkey}", retryable=True)
                continue
            adapter = self.adapter(provider)
            preq = self._provider_request(plan.request, target)
            try:
                resp = await adapter.complete(preq)
            except ProviderError as exc:
                if not exc.retryable:
                    await self._ledger(
                        ctx,
                        req,
                        "complete",
                        status="provider_error",
                        provider=provider,
                        model=target.model,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        error=str(exc),
                    )
                    raise ProviderUnavailable(
                        f"{provider.name} rejected the request: {exc}", provider=provider.name
                    ) from exc
                self.breaker.failure(bkey)
                last_error = exc
                continue
            self.breaker.success(bkey)
            usage = resp.usage
            parsed, repaired = None, False
            if req.output_schema and resp.stop not in (Stop.refusal, Stop.tool_use):
                try:
                    parsed = parse_and_validate(resp.text, req.output_schema)
                except SchemaMismatch as first:
                    repaired = True
                    preq.messages = [
                        *preq.messages,
                        Message(role="assistant", content=resp.text or "(empty)"),
                        Message(role="user", content=repair_message(str(first))),
                    ]
                    try:
                        retry = await adapter.complete(preq)
                    except ProviderError as exc:
                        raise ProviderUnavailable(str(exc), provider=provider.name) from exc
                    usage = usage + retry.usage
                    resp = ProviderResponse(
                        retry.content, retry.stop, usage, retry.model, retry.raw_id, retry.inference_geo
                    )
                    try:
                        parsed = parse_and_validate(resp.text, req.output_schema)
                    except SchemaMismatch as second:
                        cost = cost_of(target.model, usage, provider.options.get("pricing"))
                        await self._ledger(
                            ctx,
                            req,
                            "complete",
                            status="invalid_output",
                            provider=provider,
                            model=resp.model,
                            usage=usage,
                            cost=cost,
                            latency_ms=int((time.monotonic() - started) * 1000),
                            fallback=index > 0,
                            redacted=bool(red and red.applied),
                            error=str(second),
                        )
                        raise InvalidOutput(f"output did not match its schema after one repair: {second}") from second
            content = resp.content
            if red and red.applied:
                content = [
                    b.model_copy(
                        update={"text": red.restore(b.text) if b.text else b.text, "input": red.restore_obj(b.input)}
                    )
                    for b in content
                ]
                parsed = red.restore_obj(parsed)
            cost = cost_of(target.model, usage, provider.options.get("pricing"))
            latency = int((time.monotonic() - started) * 1000)
            out = CompleteResponse(
                id=req.metadata.run_id or f"gw_{uuid.uuid4().hex[:16]}",
                provider=provider.name,
                model=resp.model,
                content=content,
                stop=resp.stop,
                usage=usage,
                cost=cost,
                latency_ms=latency,
                parsed=parsed,
                fallback_used=index > 0,
                redaction_applied=bool(red and red.applied),
                repaired=repaired,
                inference_geo=resp.inference_geo,
                refusal_category=resp.refusal_category,
                safety_flags=screen("".join(b.text or "" for b in content), self._context_text(req)),
                budget_warning=plan.budget_warning,
            )
            await self._ledger(
                ctx,
                req,
                "complete",
                status="refusal" if resp.stop == Stop.refusal else "ok",
                provider=provider,
                model=resp.model,
                usage=usage,
                cost=cost,
                latency_ms=latency,
                fallback=index > 0,
                redacted=out.redaction_applied,
                geo=resp.inference_geo,
            )
            if key and self.cache is not None and resp.stop == Stop.end:
                await self.cache.set(key, out.model_dump_json(), req.metadata.cache_ttl)
            return out
        await self._ledger(
            ctx,
            req,
            "complete",
            status="provider_error",
            provider=first_provider,
            model=first_target.model,
            latency_ms=int((time.monotonic() - started) * 1000),
            error=str(last_error),
        )
        raise ProviderUnavailable(f"all providers failed: {last_error}")

    # -- stream ---------------------------------------------------------------------------
    async def stream(self, ctx: CallContext, req: CompleteRequest) -> AsyncIterator[StreamEvent]:
        try:
            plan = await self._plan(ctx, req)
        except GatewayError as exc:
            await self._ledger(ctx, req, "stream", status=exc.code, error=exc.message)
            yield StreamEvent(event="error", error=exc.to_dict())
            return
        started = time.monotonic()
        last_error: Exception | None = None
        for index, (provider, target) in enumerate(plan.targets):
            bkey = self._breaker_key(provider, target.model)
            if not self.breaker.allow(bkey):
                last_error = ProviderError(f"circuit open for {bkey}", retryable=True)
                continue
            restorer = StreamRestorer(plan.redactor)
            emitted = False
            final: ProviderResponse | None = None
            try:
                async for chunk in self.adapter(provider).stream(self._provider_request(plan.request, target)):
                    if chunk.text:
                        text = restorer.feed(chunk.text)
                        if text:
                            emitted = True
                            yield StreamEvent(event="delta", text=text)
                    elif chunk.block is not None:
                        b = chunk.block
                        if plan.redactor:
                            b = b.model_copy(update={"input": plan.redactor.restore_obj(b.input)})
                        yield StreamEvent(event="tool_use", block=b)
                    elif chunk.final is not None:
                        final = chunk.final
            except ProviderError as exc:
                self.breaker.failure(bkey)
                last_error = exc
                if emitted or not exc.retryable:
                    break  # a fallback after partial output would mix two answers
                continue
            self.breaker.success(bkey)
            tail = restorer.flush()
            if tail:
                yield StreamEvent(event="delta", text=tail)
            assert final is not None
            content = final.content
            red = plan.redactor
            if red and red.applied:
                content = [
                    b.model_copy(
                        update={"text": red.restore(b.text) if b.text else b.text, "input": red.restore_obj(b.input)}
                    )
                    for b in content
                ]
            parsed = None
            status = "refusal" if final.stop == Stop.refusal else "ok"
            if req.output_schema and final.stop == Stop.end:
                try:
                    parsed = parse_and_validate("".join(b.text or "" for b in content), req.output_schema)
                except SchemaMismatch:
                    status = "invalid_output"
            cost = cost_of(target.model, final.usage, provider.options.get("pricing"))
            latency = int((time.monotonic() - started) * 1000)
            out = CompleteResponse(
                id=req.metadata.run_id or f"gw_{uuid.uuid4().hex[:16]}",
                provider=provider.name,
                model=final.model,
                content=content,
                stop=final.stop,
                usage=final.usage,
                cost=cost,
                latency_ms=latency,
                parsed=parsed,
                fallback_used=index > 0,
                redaction_applied=bool(red and red.applied),
                inference_geo=final.inference_geo,
                refusal_category=final.refusal_category,
                safety_flags=screen("".join(b.text or "" for b in content), self._context_text(req)),
                budget_warning=plan.budget_warning,
            )
            await self._ledger(
                ctx,
                req,
                "stream",
                status=status,
                provider=provider,
                model=final.model,
                usage=final.usage,
                cost=cost,
                latency_ms=latency,
                fallback=index > 0,
                redacted=out.redaction_applied,
                geo=final.inference_geo,
            )
            yield StreamEvent(event="usage", usage=final.usage)
            if status == "invalid_output":
                yield StreamEvent(event="error", error=InvalidOutput("output did not match its schema").to_dict())
            else:
                yield StreamEvent(event="done", response=out)
            return
        await self._ledger(ctx, req, "stream", status="provider_error", error=str(last_error))
        yield StreamEvent(event="error", error=ProviderUnavailable(f"all providers failed: {last_error}").to_dict())

    # -- embeddings ---------------------------------------------------------------------------
    async def embed(self, ctx: CallContext, req: EmbedRequest) -> EmbedResponse:
        creq = CompleteRequest(
            task_class=TaskClass.embed, messages=[], redaction_policy="minimal", metadata=req.metadata
        )
        cfg = self.store.get()
        if not cfg.feature_enabled(ctx.feature or "*", ctx.company_id):
            raise FeatureDisabled("AI is disabled for this company", feature=ctx.feature)
        try:
            # embeddings carry record text verbatim (names matter for retrieval): PII-approved providers only
            targets = self._targets(cfg, ctx, TaskClass.embed.value, needs_pii=True)
            tokens = sum(len(t) for t in req.texts) // 4 + 1
            await self._check_budgets(cfg, ctx, Decimal(0), tokens)
        except GatewayError as exc:
            await self._ledger(ctx, creq, "embed", status=exc.code, error=exc.message)
            raise
        started = time.monotonic()
        last_error: Exception | None = None
        for index, (provider, target) in enumerate(targets):
            bkey = self._breaker_key(provider, target.model)
            if not self.breaker.allow(bkey):
                continue
            try:
                resp = await self.adapter(provider).embed(
                    req.texts, target.model, input_type=req.input_type, dimensions=EMBEDDING_DIMENSIONS
                )
            except NotSupported:
                raise
            except ProviderError as exc:
                self.breaker.failure(bkey)
                last_error = exc
                if not exc.retryable:
                    break
                continue
            self.breaker.success(bkey)
            resp.cost = cost_of(target.model, resp.usage, provider.options.get("pricing"))
            await self._ledger(
                ctx,
                creq,
                "embed",
                status="ok",
                provider=provider,
                model=resp.model,
                usage=resp.usage,
                cost=resp.cost,
                latency_ms=int((time.monotonic() - started) * 1000),
                fallback=index > 0,
            )
            return resp
        await self._ledger(ctx, creq, "embed", status="provider_error", error=str(last_error))
        raise ProviderUnavailable(f"embedding providers failed: {last_error}")

    # -- misc ---------------------------------------------------------------------------------
    @staticmethod
    def moderate(req: ModerateRequest) -> ModerateResponse:
        return moderate(req.text, req.context)

    async def usage(
        self, *, company_id: int | None, since: dt.datetime | None, until: dt.datetime | None, group_by: list[str]
    ) -> list[dict[str, Any]]:
        return await self.ledger.report(company_id=company_id, since=since, until=until, group_by=group_by)

    def health(self) -> dict[str, Any]:
        cfg = self.store.get()
        return {
            "status": "ok",
            "ai_enabled": cfg.global_enabled,
            "providers": [
                {"id": p.id, "name": p.name, "kind": p.kind, "region": p.region, "pii_approved": p.approved_for_pii}
                for p in cfg.providers.values()
            ],
            "circuits": self.breaker.snapshot(),
        }

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()
