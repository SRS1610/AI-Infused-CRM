"""List prices per million tokens, used for the ledger's cost column and budget checks.

Provider configurations may override any entry (``options.pricing``) for negotiated rates or
self-hosted models (whose cost is usually zero). Unknown models cost zero and are flagged in the
ledger so an administrator notices.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from gateway.envelope import Usage

MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    input: Decimal
    output: Decimal
    cache_read: Decimal | None = None
    cache_write: Decimal | None = None

    def cost(self, usage: Usage) -> Decimal:
        cache_read = self.cache_read if self.cache_read is not None else self.input / 10
        cache_write = self.cache_write if self.cache_write is not None else self.input * Decimal("1.25")
        total = (
            self.input * usage.input_tokens
            + self.output * usage.output_tokens
            + cache_read * usage.cache_read_tokens
            + cache_write * usage.cache_write_tokens
        ) / MTOK
        return total.quantize(Decimal("0.000001"))


def _p(i: str, o: str, cr: str | None = None, cw: str | None = None) -> Price:
    return Price(Decimal(i), Decimal(o), Decimal(cr) if cr else None, Decimal(cw) if cw else None)


# Checked against the providers' published list prices when the adapters were pinned (2026-11).
DEFAULT_PRICES: dict[str, Price] = {
    "claude-haiku-5-5": _p("0.10", "0.50", "0.01"),
    "claude-sonnet-5-5": _p("2", "10", "0.10"),
    "claude-opus-5-5": _p("4", "20", "0.20"),
    "text-embedding-3-small": _p("0.02", "0"),
    "text-embedding-3-large": _p("0.13", "0"),
    "voyage-3.5": _p("0.06", "0"),
    "voyage-3.5-lite": _p("0.02", "0"),
    # deterministic test adapter: a non-zero price so cost reporting is exercised end to end
    "mock-small": _p("1", "2"),
    "mock-mid": _p("3", "6"),
    "mock-large": _p("5", "10"),
    "mock-embed": _p("0.5", "0"),
}


def price_for(model: str, overrides: dict[str, Any] | None = None) -> Price | None:
    if overrides and model in overrides:
        o = overrides[model]
        return _p(str(o.get("input", 0)), str(o.get("output", 0)), o.get("cache_read"), o.get("cache_write"))
    return DEFAULT_PRICES.get(model)


def cost_of(model: str, usage: Usage, overrides: dict[str, Any] | None = None) -> Decimal:
    price = price_for(model, overrides)
    return price.cost(usage) if price else Decimal(0)
