"""Process configuration.

Everything is read from environment variables prefixed ``TESSERA_`` (or a ``.env``
file in development). Secrets are never defaulted in production: :func:`Settings.check`
refuses to start with placeholder values when ``TESSERA_ENV=production``.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BACKEND_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MODULE_PATH = BACKEND_ROOT / "modules"

Env = Literal["development", "test", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TESSERA_", env_file=".env", extra="ignore")

    env: Env = "development"

    # database
    database_url: str = "postgresql://tessera:tessera@localhost:5432/tessera"
    test_database_url: str = "postgresql://tessera:tessera@localhost:5432/tessera_test"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_statement_timeout_ms: int = 60_000

    # redis
    redis_url: str = "redis://localhost:6379/0"

    # web
    secret_key: str = "dev-only-secret-key-change-me-please-32b"  # noqa: S105 - rejected by check() in production
    master_key: str = "ZGV2LW9ubHktbWFzdGVyLWtleS0zMi1ieXRlcy0hIQ=="
    base_url: str = "http://localhost:8000"
    allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["localhost", "127.0.0.1"])
    session_idle_minutes: int = 60 * 8
    session_absolute_hours: int = 24 * 7

    # modules
    module_paths: Annotated[list[Path], NoDecode] = Field(default_factory=lambda: [DEFAULT_MODULE_PATH])
    default_lang: str = "en_US"
    default_tz: str = "UTC"
    admin_password: str | None = None  # initial password of the `admin` user; "admin1234" outside production

    # storage
    storage_backend: Literal["s3", "filesystem"] = "filesystem"
    storage_path: Path = BACKEND_ROOT / "var" / "storage"
    s3_endpoint: str | None = None
    s3_bucket: str = "tessera"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    # mail
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_tls: bool = False
    mail_from: str = "noreply@tessera.local"

    # AI gateway
    ai_gateway_url: str = "http://localhost:8100"
    ai_gateway_token: str = "dev-only-gateway-token"  # noqa: S105 - rejected by check() in production

    # logging / observability
    log_level: str = "INFO"
    log_json: bool | None = None  # None → JSON in production, console otherwise
    otel_exporter_endpoint: str | None = None

    @field_validator("module_paths", mode="before")
    @classmethod
    def _split_paths(cls, value: object) -> object:
        if isinstance(value, str):
            return [Path(p).expanduser() for p in value.split(":") if p]
        return value

    @field_validator("allowed_hosts", mode="before")
    @classmethod
    def _split_hosts(cls, value: object) -> object:
        if isinstance(value, str):
            return [h.strip() for h in value.split(",") if h.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def master_key_bytes(self) -> bytes:
        return base64.b64decode(self.master_key)

    def check(self) -> None:
        """Refuse to run production with development placeholders."""
        if not self.is_production:
            return
        problems = []
        if "dev-only" in self.secret_key or len(self.secret_key) < 32:
            problems.append("TESSERA_SECRET_KEY must be a random value of at least 32 characters")
        if len(self.master_key_bytes) < 32:
            problems.append("TESSERA_MASTER_KEY must decode to at least 32 bytes")
        if "dev-only" in self.ai_gateway_token:
            problems.append("TESSERA_AI_GATEWAY_TOKEN must be set")
        if problems:
            raise RuntimeError("Unsafe production configuration:\n - " + "\n - ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Used by tests that change environment variables."""
    get_settings.cache_clear()
