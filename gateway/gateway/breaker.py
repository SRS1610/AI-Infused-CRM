"""Circuit breakers per (provider, model): ``threshold`` consecutive retryable failures open the
circuit for ``cooldown`` seconds; then one trial call is let through (half-open). State is kept per
gateway process (D-046)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _State:
    failures: int = 0
    opened_at: float | None = None
    trial: bool = False


class CircuitBreaker:
    def __init__(self, threshold: int = 3, cooldown: float = 30.0) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._states: dict[str, _State] = {}

    def _s(self, key: str) -> _State:
        return self._states.setdefault(key, _State())

    def allow(self, key: str) -> bool:
        s = self._s(key)
        if s.opened_at is None:
            return True
        if time.monotonic() - s.opened_at >= self.cooldown and not s.trial:
            s.trial = True  # half-open: one call goes through
            return True
        return False

    def success(self, key: str) -> None:
        self._states[key] = _State()

    def failure(self, key: str) -> None:
        s = self._s(key)
        s.failures += 1
        s.trial = False
        if s.failures >= self.threshold:
            s.opened_at = time.monotonic()

    def state(self, key: str) -> str:
        s = self._s(key)
        if s.opened_at is None:
            return "closed"
        return "half_open" if time.monotonic() - s.opened_at >= self.cooldown else "open"

    def snapshot(self) -> dict[str, Any]:
        return {k: {"state": self.state(k), "failures": v.failures} for k, v in self._states.items()}
