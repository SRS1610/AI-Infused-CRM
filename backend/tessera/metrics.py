"""Prometheus metrics of the API process (U-10): HTTP requests, background jobs and AI feature runs.

Exposed on ``GET /metrics`` (``tessera.api.routers.health``), together with the gateway's own metrics when
the gateway runs in-process. A separate gateway service exposes its metrics on its own ``/metrics``.
"""

from __future__ import annotations

from collections.abc import Mapping

from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()

HTTP_REQUESTS = Counter(
    "tessera_http_requests", "API requests by route and status class", ["method", "route", "status"], registry=REGISTRY
)
HTTP_LATENCY = Histogram(
    "tessera_http_request_seconds",
    "API request duration",
    ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
    registry=REGISTRY,
)
JOBS = Counter(
    "tessera_jobs", "Background job attempts by outcome (done, retry, dead)", ["job", "state"], registry=REGISTRY
)
JOB_SECONDS = Histogram(
    "tessera_job_seconds",
    "Background job run time",
    ["job"],
    buckets=(0.05, 0.1, 0.5, 1, 5, 15, 60, 300, 1800),
    registry=REGISTRY,
)
AI_RUNS = Counter("tessera_ai_runs", "AI feature runs by outcome", ["feature", "status"], registry=REGISTRY)
AI_TOKENS = Counter(
    "tessera_ai_tokens", "AI tokens by feature and direction", ["feature", "direction"], registry=REGISTRY
)
AI_COST = Counter("tessera_ai_cost_usd", "AI provider cost by feature (USD)", ["feature"], registry=REGISTRY)
AI_LATENCY = Histogram(
    "tessera_ai_latency_seconds",
    "Provider time of an AI feature run",
    ["feature"],
    buckets=(0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30, 60),
    registry=REGISTRY,
)


def record_ai_run(feature: str, status: str, usage: Mapping[str, object] | None = None) -> None:
    AI_RUNS.labels(feature, status).inc()
    if not usage:
        return
    for direction, key in (("input", "input_tokens"), ("output", "output_tokens"), ("cache_read", "cache_read_tokens")):
        value = usage.get(key)
        if isinstance(value, int | float) and value:
            AI_TOKENS.labels(feature, direction).inc(value)
    cost = usage.get("cost")
    if isinstance(cost, int | float) and cost:
        AI_COST.labels(feature).inc(float(cost))
    latency = usage.get("latency_ms")
    if isinstance(latency, int | float) and latency:
        AI_LATENCY.labels(feature).observe(latency / 1000)


def exposition() -> bytes:
    """The text exposition of every registry this process owns."""
    from prometheus_client import generate_latest

    data = generate_latest(REGISTRY)
    try:
        from gateway import metrics as gateway_metrics

        data += generate_latest(gateway_metrics.REGISTRY)
    except ImportError:  # pragma: no cover - gateway library not installed
        pass
    return data
