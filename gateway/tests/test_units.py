"""Redaction rules, validation, breaker recovery, cache expiry, pricing, routing defaults."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import pytest

from gateway.adapters.base import fit_dimensions
from gateway.adapters.mock import hash_embedding, synthesize
from gateway.breaker import CircuitBreaker
from gateway.cache import MemoryCache, cache_key
from gateway.config import GatewayConfig, ProviderConfig, default_routes_for
from gateway.envelope import Usage
from gateway.pricing import cost_of, price_for
from gateway.redaction import Redactor, StreamRestorer, leaked_placeholders
from gateway.validation import SchemaMismatch, parse_and_validate


def test_redaction_kinds_and_stable_placeholders() -> None:
    r = Redactor(["Dana Reyes", "Acme Robotics", "Al"])
    text = (
        "Dana Reyes (dana@acme.example) from Acme Robotics paid with 4111 1111 1111 1111, IBAN DE89 3704 0044 0532 0130 00, "
        "SSN 123-45-6789, phone (415) 555-0134. Dr. Watson agreed. Dana will call. Ref 2026-11-10, qty 1234."
    )
    out = r.redact(text)
    for secret in ("Dana", "Reyes", "dana@acme.example", "4111", "DE89", "123-45-6789", "555-0134", "Watson", "Acme"):
        assert secret not in out
    assert "2026-11-10" in out and "1234" in out  # dates and short numbers are not phones
    assert out.count("[PERSON_1]") == 1 and "[EMAIL_1]" in out and "[CARD_1]" in out and "[IBAN_1]" in out
    assert r.restore(out) == text
    assert r.restore("Thanks PERSON_1") == "Thanks Dana Reyes"  # brackets dropped by the model
    assert r.restore_obj({"a": ["[EMAIL_1]"], "n": 3}) == {"a": ["dana@acme.example"], "n": 3}


def test_card_requires_luhn() -> None:
    r = Redactor()
    assert "[CARD_1]" not in r.redact("order 4111 1111 1111 1112")


def test_stream_restorer_holds_partial_placeholders() -> None:
    r = Redactor(["Dana Reyes"])
    r.redact("Dana Reyes")
    s = StreamRestorer(r)
    assert s.feed("Hello [PER") == "Hello "
    assert s.feed("SON_1], wel") == "Dana Reyes, wel"
    assert s.feed("come [") == "come "
    assert s.flush() == "["
    assert StreamRestorer(None).feed("x [") == "x ["
    assert leaked_placeholders("hi [EMAIL_9] and [PERSON_2]") == ["[EMAIL_9]", "[PERSON_2]"]


def test_validation_accepts_fenced_json_and_reports_paths() -> None:
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert parse_and_validate('```json\n{"n": 2}\n```', schema) == {"n": 2}
    with pytest.raises(SchemaMismatch, match="not valid JSON"):
        parse_and_validate("{", schema)
    with pytest.raises(SchemaMismatch, match="n: 'x' is not of type 'integer'"):
        parse_and_validate('{"n": "x"}', schema)


def test_breaker_half_open_and_recovery() -> None:
    b = CircuitBreaker(threshold=2, cooldown=0.05)
    b.failure("k")
    assert b.allow("k") and b.state("k") == "closed"
    b.failure("k")
    assert not b.allow("k") and b.state("k") == "open"
    time.sleep(0.06)
    assert b.state("k") == "half_open"
    assert b.allow("k") and not b.allow("k")  # one trial only
    b.success("k")
    assert b.state("k") == "closed" and b.snapshot()["k"]["failures"] == 0


async def test_memory_cache_expiry_and_eviction() -> None:
    c = MemoryCache(max_entries=2)
    await c.set("a", "1", 1)
    await c.set("b", "2", 1)
    await c.set("c", "3", 1)
    assert await c.get("a") is None and await c.get("c") == "3"
    await c.set("d", "4", 0)
    await asyncio.sleep(0.01)
    assert await c.get("d") is None
    assert cache_key(1, "p@1", "m", {"x": 1}) == cache_key(1, "p@1", "m", {"x": 1})
    assert cache_key(1, "p@1", "m", {"x": 1}) != cache_key(2, "p@1", "m", {"x": 1})


def test_pricing() -> None:
    u = Usage(
        input_tokens=1_000_000, output_tokens=1_000_000, cache_read_tokens=1_000_000, cache_write_tokens=1_000_000
    )
    assert cost_of("claude-opus-5-5", u) == Decimal("4") + Decimal("20") + Decimal("0.20") + Decimal("5")
    assert cost_of("unknown-model", u) == 0
    assert cost_of("local", Usage(input_tokens=2_000_000), {"local": {"input": 0.5, "output": 1}}) == Decimal("1")
    assert price_for("claude-haiku-5-5") is not None


def test_default_routes_and_vectors() -> None:
    p = ProviderConfig(id=3, name="claude", kind="anthropic")
    routes = default_routes_for(p)
    cfg = GatewayConfig(routes=routes)
    assert cfg.route_for(1, "classify").primary.model == "claude-haiku-5-5"  # type: ignore[union-attr]
    assert cfg.route_for(1, "reason").primary.model == "claude-opus-5-5"  # type: ignore[union-attr]
    assert cfg.route_for(1, "embed") is None  # Anthropic has no embeddings
    assert fit_dimensions([1.0, 0.0], 4) == [1.0, 0.0, 0.0, 0.0]
    assert fit_dimensions([3.0, 4.0, 1.0], 2) == [0.6, 0.8]
    a, b, c = (hash_embedding(t, 64) for t in ("acme robotics cobots", "acme robotics order", "invoice overdue"))
    assert sum(x * y for x, y in zip(a, b, strict=True)) > sum(x * y for x, y in zip(a, c, strict=True))
    schema = {
        "type": "object",
        "properties": {
            "d": {"type": "string", "format": "date"},
            "e": {"enum": ["x", "y"]},
            "l": {"type": "array", "items": {"type": "integer"}, "minItems": 1},
            "o": {"anyOf": [{"type": "boolean"}, {"type": "null"}]},
            "n": {"type": ["null", "number"], "minimum": 2},
        },
    }
    assert synthesize(schema) == {"d": "2026-01-01", "e": "x", "l": [0], "o": False, "n": 2.0}
