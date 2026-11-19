from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from tessera.exceptions import ConfigurationError

JobFunc = Callable[..., Any]


@dataclass(frozen=True)
class JobSpec:
    name: str
    func: JobFunc
    queue: str = "default"
    max_attempts: int = 5
    backoff_seconds: int = 30  # attempt n waits backoff * 2^(n-1), capped at 1 hour
    timeout_seconds: int = 600
    as_system: bool = False  # run with an elevated environment (platform maintenance jobs only)

    def delay_for(self, attempt: int) -> int:
        return int(min(self.backoff_seconds * 2 ** max(attempt - 1, 0), 3600))


_jobs: dict[str, JobSpec] = {}


def job(
    name: str,
    *,
    queue: str = "default",
    max_attempts: int = 5,
    backoff_seconds: int = 30,
    timeout_seconds: int = 600,
    as_system: bool = False,
) -> Callable[[JobFunc], JobFunc]:
    def decorator(func: JobFunc) -> JobFunc:
        existing = _jobs.get(name)
        if existing is not None and existing.func.__qualname__ != func.__qualname__:
            raise ConfigurationError(f"Job '{name}' is already registered by {existing.func.__module__}")
        _jobs[name] = JobSpec(name, func, queue, max_attempts, backoff_seconds, timeout_seconds, as_system)
        return func

    return decorator


def get_job(name: str) -> JobSpec:
    try:
        return _jobs[name]
    except KeyError as exc:
        raise ConfigurationError(f"Unknown job '{name}'") from exc


def registered_jobs() -> dict[str, JobSpec]:
    return dict(_jobs)
