"""Gateway configuration: providers, routes, budgets, feature flags and company AI settings.

The source of truth is the ``ai_core`` module's tables in the application database (edited by
administrators in the application UI); the gateway only reads them (``SqlConfigStore``), caching
for a few seconds so a kill switch takes effect quickly. ``StaticConfigStore`` serves tests and
single-provider deployments configured by environment.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from gateway.envelope import TaskClass

PROVIDER_KINDS = ("anthropic", "openai", "azure_openai", "google", "openai_compatible", "voyage", "mock")

# Default model per tier for providers that have tiers (D-016).
TIERS: dict[str, dict[str, str]] = {
    "anthropic": {"small": "claude-haiku-5-5", "mid": "claude-sonnet-5-5", "large": "claude-opus-5-5"},
    "mock": {"small": "mock-small", "mid": "mock-mid", "large": "mock-large", "embed": "mock-embed"},
    "openai": {"embed": "text-embedding-3-small"},
    "voyage": {"embed": "voyage-3.5"},
}
TASK_TIER: dict[TaskClass, str] = {
    TaskClass.classify: "small",
    TaskClass.extract: "small",
    TaskClass.translation: "small",
    TaskClass.nl_query: "small",
    TaskClass.summarize: "mid",
    TaskClass.draft: "mid",
    TaskClass.vision: "mid",
    TaskClass.reason: "large",
    TaskClass.embed: "embed",
    TaskClass.transcribe: "mid",
}


@dataclass
class ProviderConfig:
    id: int
    name: str
    kind: str
    api_key: str | None = None
    base_url: str | None = None
    region: str | None = None  # residency label (e.g. "us", "eu", "self-hosted")
    approved_for_pii: bool = False
    retention: str = ""  # human description of the provider's data-retention regime
    options: dict[str, Any] = field(default_factory=dict)
    active: bool = True


@dataclass
class Target:
    provider_id: int
    model: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class Route:
    company_id: int | None  # None → default for every company without its own route
    task_class: str
    primary: Target
    fallback: Target | None = None


@dataclass
class Budget:
    id: int
    company_id: int
    period: str = "month"  # day | month
    user_id: int | None = None  # per-user quota when set
    feature: str | None = None  # per-feature cap when set
    limit_cost: Decimal | None = None
    limit_tokens: int | None = None
    limit_requests: int | None = None
    hard: bool = True
    alert_pct: int = 80


@dataclass
class CompanySettings:
    company_id: int
    ai_enabled: bool = True
    allowed_regions: list[str] = field(default_factory=list)  # empty → any region


@dataclass
class GatewayConfig:
    providers: dict[int, ProviderConfig] = field(default_factory=dict)
    routes: list[Route] = field(default_factory=list)
    budgets: list[Budget] = field(default_factory=list)
    # feature name → {company_id or None: enabled}
    flags: dict[str, dict[int | None, bool]] = field(default_factory=dict)
    companies: dict[int, CompanySettings] = field(default_factory=dict)
    global_enabled: bool = True

    def route_for(self, company_id: int, task_class: str) -> Route | None:
        own = [r for r in self.routes if r.company_id == company_id and r.task_class == task_class]
        if own:
            return own[0]
        default = [r for r in self.routes if r.company_id is None and r.task_class == task_class]
        return default[0] if default else None

    def feature_enabled(self, feature: str, company_id: int) -> bool:
        """Kill switches: global, per company (all AI), per feature globally, per feature per company.
        A feature unknown to the gateway is allowed (the runtime registers features it calls)."""
        if not self.global_enabled:
            return False
        settings = self.companies.get(company_id)
        if settings is not None and not settings.ai_enabled:
            return False
        flags = self.flags.get(feature)
        if not flags:
            return True
        if None in flags and not flags[None]:
            return False
        return flags.get(company_id, flags.get(None, True))

    def budgets_for(self, company_id: int) -> Iterable[Budget]:
        return (b for b in self.budgets if b.company_id == company_id)


class ConfigStore(Protocol):
    def get(self) -> GatewayConfig: ...


class StaticConfigStore:
    def __init__(self, config: GatewayConfig) -> None:
        self.config = config

    def get(self) -> GatewayConfig:
        return self.config


def default_routes_for(provider: ProviderConfig, company_id: int | None = None) -> list[Route]:
    """One route per task class to the provider's tier models (used for env-configured setups)."""
    tiers = TIERS.get(provider.kind, {})
    routes = []
    for task, tier in TASK_TIER.items():
        model = tiers.get(tier) or provider.options.get(f"model_{tier}") or provider.options.get("model")
        if model:
            routes.append(Route(company_id, task.value, Target(provider.id, model)))
    return routes


class SqlConfigStore:
    """Reads the ``ai_core`` tables. Values are cached for ``ttl`` seconds; API keys are decrypted with
    the shared master key (same envelope as the application)."""

    def __init__(self, database_url: str, *, master_key: str, ttl: float = 3.0, env_keys: dict[str, str] | None = None):
        self.database_url = database_url
        self.master_key = master_key
        self.ttl = ttl
        self.env_keys = env_keys or {}
        self._cached: GatewayConfig | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def invalidate(self) -> None:
        self._at = 0.0

    def get(self) -> GatewayConfig:
        with self._lock:
            if self._cached is None or time.monotonic() - self._at > self.ttl:
                self._cached = self._load()
                self._at = time.monotonic()
            return self._cached

    def _load(self) -> GatewayConfig:
        import psycopg
        from psycopg.rows import dict_row

        from gateway.crypto import decrypt

        cfg = GatewayConfig()
        with psycopg.connect(self.database_url, row_factory=dict_row, autocommit=True) as conn:
            tables = {
                r["t"] for r in conn.execute("SELECT tablename AS t FROM pg_tables WHERE tablename LIKE 'ai_core_%'")
            }
            if "ai_core_provider" not in tables:
                return cfg
            for r in conn.execute("SELECT * FROM ai_core_provider WHERE active"):
                key = decrypt(r.get("api_key"), self.master_key) or self.env_keys.get(r["kind"])
                cfg.providers[r["id"]] = ProviderConfig(
                    id=r["id"],
                    name=r["name"],
                    kind=r["kind"],
                    api_key=key,
                    base_url=r.get("base_url") or None,
                    region=r.get("region") or None,
                    approved_for_pii=bool(r.get("approved_for_pii")),
                    retention=r.get("retention") or "",
                    options=r.get("options") or {},
                )
            for r in conn.execute("SELECT * FROM ai_core_route WHERE active ORDER BY company_id NULLS LAST, id"):
                fallback = (
                    Target(r["fallback_provider_id"], r["fallback_model"] or "", r.get("fallback_params") or {})
                    if r.get("fallback_provider_id")
                    else None
                )
                cfg.routes.append(
                    Route(
                        r.get("company_id"),
                        r["task_class"],
                        Target(r["provider_id"], r["model"], r.get("params") or {}),
                        fallback,
                    )
                )
            if "ai_core_budget" in tables:
                for r in conn.execute("SELECT * FROM ai_core_budget WHERE active"):
                    cfg.budgets.append(
                        Budget(
                            id=r["id"],
                            company_id=r["company_id"],
                            period=r.get("period") or "month",
                            user_id=r.get("user_id"),
                            feature=r.get("feature") or None,
                            limit_cost=r.get("limit_cost"),
                            limit_tokens=r.get("limit_tokens") or None,
                            limit_requests=r.get("limit_requests") or None,
                            hard=bool(r.get("hard", True)),
                            alert_pct=r.get("alert_pct") or 80,
                        )
                    )
            if "ai_core_feature_flag" in tables and "ai_core_feature" in tables:
                rows = conn.execute(
                    "SELECT f.name, ff.company_id, ff.enabled FROM ai_core_feature_flag ff "
                    "JOIN ai_core_feature f ON f.id = ff.feature_id"
                )
                for r in rows:
                    cfg.flags.setdefault(r["name"], {})[r["company_id"]] = bool(r["enabled"])
            if "ai_core_company_setting" in tables:
                for r in conn.execute("SELECT * FROM ai_core_company_setting"):
                    if r["company_id"] is None:
                        cfg.global_enabled = bool(r.get("ai_enabled", True))
                        continue
                    cfg.companies[r["company_id"]] = CompanySettings(
                        r["company_id"], bool(r.get("ai_enabled", True)), list(r.get("allowed_regions") or [])
                    )
        return cfg
