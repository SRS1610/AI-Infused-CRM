"""Prometheus metrics (U-10): request count, latency, failures, tokens and cost per feature."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram

REGISTRY = CollectorRegistry()
REQUESTS = Counter(
    "tessera_ai_gateway_requests",
    "Gateway calls by outcome",
    ["feature", "provider", "model", "status"],
    registry=REGISTRY,
)
TOKENS = Counter(
    "tessera_ai_gateway_tokens", "Tokens by direction", ["feature", "provider", "model", "direction"], registry=REGISTRY
)
COST = Counter(
    "tessera_ai_gateway_cost_usd", "Provider cost in USD", ["feature", "provider", "model"], registry=REGISTRY
)
LATENCY = Histogram(
    "tessera_ai_gateway_latency_seconds",
    "Provider latency",
    ["feature", "provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 3, 5, 10, 30, 60),
    registry=REGISTRY,
)
FALLBACKS = Counter("tessera_ai_gateway_fallbacks", "Calls served by a fallback route", ["feature"], registry=REGISTRY)
