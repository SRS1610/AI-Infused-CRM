"""Gateway settings (environment variables prefixed ``GATEWAY_``)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="GATEWAY_", env_file=".env", extra="ignore")

    env: str = "development"
    service_token: str = "dev-only-gateway-token"  # noqa: S105 - refused in production by check()
    database_url: str = ""  # empty → static configuration from the variables below, in-memory ledger
    redis_url: str = ""
    master_key: str = "ZGV2LW9ubHktbWFzdGVyLWtleS1jaGFuZ2UtbWUtMzItYnl0ZXMh"
    config_ttl_seconds: float = 3.0
    # environment-provided keys, used when a provider row stores no key of its own
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    google_api_key: str = ""
    voyage_api_key: str = ""
    ollama_base_url: str = ""
    # static mode: which provider serves every task class
    static_provider: str = "mock"  # mock | anthropic | openai_compatible
    breaker_threshold: int = 3
    breaker_cooldown_seconds: float = 30.0

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def env_keys(self) -> dict[str, str]:
        return {
            k: v
            for k, v in {
                "anthropic": self.anthropic_api_key,
                "openai": self.openai_api_key,
                "google": self.google_api_key,
                "voyage": self.voyage_api_key,
            }.items()
            if v
        }

    def check(self) -> None:
        if self.is_production and ("dev-only" in self.service_token or len(self.service_token) < 24):
            raise RuntimeError("GATEWAY_SERVICE_TOKEN must be set to a random value in production")


@lru_cache
def get_settings() -> Settings:
    return Settings()
