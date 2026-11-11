"""Gateway pipeline: routing, fallback, budgets, redaction, validation, kill switches, cache, stream."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from gateway.adapters.mock import MockAdapter
from gateway.breaker import CircuitBreaker
from gateway.cache import MemoryCache
from gateway.config import Budget, CompanySettings, GatewayConfig, ProviderConfig, Route, StaticConfigStore, Target
from gateway.envelope import CompleteRequest, EmbedRequest, Message, Meta, ModerateRequest, Stop, TaskClass, ToolSpec
from gateway.errors import (
    BudgetExceeded,
    FeatureDisabled,
    InvalidOutput,
    PolicyRefused,
    ProviderUnavailable,
    QuotaExceeded,
)
from gateway.ledger import MemoryLedger
from gateway.service import CallContext, Gateway

SCHEMA = {
    "type": "object",
    "properties": {"label": {"type": "string", "enum": ["hot", "cold"]}, "score": {"type": "integer"}},
    "required": ["label", "score"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def _reset_mock() -> None:
    MockAdapter.reset()


def mock_provider(pid: int, name: str, **options: Any) -> ProviderConfig:
    return ProviderConfig(
        id=pid,
        name=name,
        kind="mock",
        region=options.pop("region", "eu"),
        approved_for_pii=options.pop("pii", True),
        options=options,
    )


def make(
    providers: list[ProviderConfig], routes: list[Route], *, budgets: list[Budget] | None = None, **cfg: Any
) -> tuple[Gateway, MemoryLedger, GatewayConfig]:
    config = GatewayConfig(providers={p.id: p for p in providers}, routes=routes, budgets=budgets or [], **cfg)
    ledger = MemoryLedger()
    gw = Gateway(
        StaticConfigStore(config), ledger, cache=MemoryCache(), breaker=CircuitBreaker(threshold=3, cooldown=60)
    )
    return gw, ledger, config


def req(text: str = "Classify this lead", task: TaskClass = TaskClass.classify, **kw: Any) -> CompleteRequest:
    meta = kw.pop("metadata", Meta(feature="crm.test", prompt_version="crm.test@1"))
    return CompleteRequest(task_class=task, messages=[Message(role="user", content=text)], metadata=meta, **kw)


def route(
    task: str, pid: int, model: str, company: int | None = None, fallback: tuple[int, str] | None = None
) -> Route:
    return Route(company, task, Target(pid, model), Target(*fallback) if fallback else None)


async def test_routing_per_company_hits_only_the_configured_provider() -> None:
    """1.5-b: company A → provider A, company B → self-hosted provider B; never the other."""
    a = mock_provider(1, "anthropic-like", region="us")
    b = mock_provider(2, "ollama-like", region="self-hosted")
    gw, ledger, _ = make(
        [a, b], [route("classify", 1, "mock-small", company=1), route("classify", 2, "mock-small", company=2)]
    )
    ra = await gw.complete(CallContext(1, 10, "crm.test"), req())
    rb = await gw.complete(CallContext(2, 20, "crm.test"), req())
    assert (ra.provider, rb.provider) == ("anthropic-like", "ollama-like")
    by_company = {(e.company_id, e.provider) for e in ledger.entries}
    assert by_company == {(1, "anthropic-like"), (2, "ollama-like")}


async def test_default_route_applies_when_company_has_none() -> None:
    gw, _, _ = make([mock_provider(1, "shared")], [route("summarize", 1, "mock-mid")])
    out = await gw.complete(CallContext(7), req(task=TaskClass.summarize))
    assert out.model == "mock-mid"


async def test_fallback_after_three_failures_opens_circuit() -> None:
    """1.5-c: three consecutive 5xx open the circuit; the next call is served by the secondary."""
    primary = mock_provider(1, "primary", fail_status=503, fail_times=-1)
    secondary = mock_provider(2, "secondary")
    gw, ledger, _ = make([primary, secondary], [route("classify", 1, "mock-small", fallback=(2, "mock-small"))])
    for _ in range(3):
        out = await gw.complete(CallContext(1), req())
        assert out.provider == "secondary" and out.fallback_used
    assert gw.breaker.state("primary:mock-small") == "open"
    calls_before = MockAdapter._calls.get(1, 0)
    out = await gw.complete(CallContext(1), req())
    assert out.provider == "secondary" and out.fallback_used
    assert MockAdapter._calls.get(1, 0) == calls_before  # circuit open: primary not even tried
    assert all(e.fallback for e in ledger.entries if e.status == "ok")


async def test_all_providers_down_raises_unavailable() -> None:
    gw, ledger, _ = make([mock_provider(1, "p", fail_status=500, fail_times=-1)], [route("classify", 1, "mock-small")])
    with pytest.raises(ProviderUnavailable):
        await gw.complete(CallContext(1), req())
    assert ledger.entries[-1].status == "provider_error"


async def test_non_retryable_error_is_not_retried() -> None:
    p1 = mock_provider(1, "p1", fail_status=400, fail_times=-1)
    gw, _, _ = make([p1, mock_provider(2, "p2")], [route("classify", 1, "mock-small", fallback=(2, "mock-small"))])
    with pytest.raises(ProviderUnavailable, match="rejected"):
        await gw.complete(CallContext(1), req())
    assert MockAdapter._calls.get(2) is None


async def test_company_budget_blocks_without_provider_call() -> None:
    """1.5-d: a company at 100 % of its monthly spend gets budget_exceeded; no provider call."""
    gw, ledger, _ = make(
        [mock_provider(1, "p")],
        [route("classify", 1, "mock-small")],
        budgets=[Budget(1, company_id=1, limit_cost=Decimal("0.00025"))],
    )
    first = await gw.complete(CallContext(1, 5), req("x" * 800))  # estimate fits, actual spend reaches the budget
    assert first.cost >= Decimal("0.00025")
    calls = MockAdapter._calls[1]
    with pytest.raises(BudgetExceeded):
        await gw.complete(CallContext(1, 5), req())
    assert MockAdapter._calls[1] == calls
    assert ledger.entries[-1].status == "budget_exceeded"
    # another company is unaffected
    assert (await gw.complete(CallContext(2, 6), req())).stop == Stop.end


async def test_user_quota_blocks_one_user_only() -> None:
    gw, _, _ = make(
        [mock_provider(1, "p")],
        [route("classify", 1, "mock-small")],
        budgets=[Budget(2, company_id=1, user_id=5, period="day", limit_requests=2)],
    )
    for _ in range(2):
        await gw.complete(CallContext(1, 5), req())
    with pytest.raises(QuotaExceeded):
        await gw.complete(CallContext(1, 5), req())
    assert (await gw.complete(CallContext(1, 6), req())).stop == Stop.end


async def test_soft_budget_warns_but_serves() -> None:
    gw, _, _ = make(
        [mock_provider(1, "p")],
        [route("classify", 1, "mock-small")],
        budgets=[Budget(3, company_id=1, limit_cost=Decimal("0.00001"), hard=False)],
    )
    await gw.complete(CallContext(1), req("y" * 400))
    out = await gw.complete(CallContext(1), req())
    assert out.budget_warning


async def test_redaction_replaces_pii_and_restores_output() -> None:
    """1.5-e: emails, phones and names become placeholders in the provider request; the answer is restored."""
    p = mock_provider(
        1,
        "hosted",
        pii=False,
        script=[{"match": {"contains": "Draft"}, "respond": {"text": "Hello [PERSON_1], we will call [PHONE_1]."}}],
    )
    gw, ledger, _ = make([p], [route("draft", 1, "mock-mid")])
    text = "Draft a reply to Jordan Lee (jordan.lee@acme.example, +1 415 555 0134) about the quote."
    out = await gw.complete(
        CallContext(1),
        req(text, task=TaskClass.draft, metadata=Meta(feature="crm.draft", redaction_names=["Jordan Lee"])),
    )
    sent = json.dumps(MockAdapter.captured[-1]["messages"])
    for secret in ("Jordan", "Lee", "jordan.lee@acme.example", "415 555 0134"):
        assert secret not in sent
    assert "[EMAIL_1]" in sent and "[PHONE_1]" in sent and "[PERSON_1]" in sent
    assert out.text == "Hello Jordan Lee, we will call +1 415 555 0134."
    assert out.redaction_applied and ledger.entries[-1].redacted


async def test_minimal_redaction_requires_pii_approved_provider() -> None:
    hosted = mock_provider(1, "hosted", pii=False)
    gw, _, _ = make([hosted], [route("draft", 1, "mock-mid")])
    with pytest.raises(PolicyRefused, match="not approved"):
        await gw.complete(CallContext(1), req(task=TaskClass.draft, redaction_policy="minimal"))
    approved = mock_provider(2, "approved")
    gw, _, _ = make([hosted, approved], [route("draft", 1, "mock-mid", fallback=(2, "mock-mid"))])
    out = await gw.complete(CallContext(1), req(task=TaskClass.draft, redaction_policy="minimal"))
    assert out.provider == "approved"


async def test_residency_excludes_providers_outside_allowed_regions() -> None:
    us = mock_provider(1, "us", region="us")
    eu = mock_provider(2, "eu", region="eu")
    gw, _, _ = make(
        [us, eu],
        [route("classify", 1, "mock-small", fallback=(2, "mock-small"))],
        companies={1: CompanySettings(1, allowed_regions=["eu"])},
    )
    assert (await gw.complete(CallContext(1), req())).provider == "eu"
    assert (await gw.complete(CallContext(2), req())).provider == "us"


async def test_schema_repair_once_then_succeed() -> None:
    """1.5-f: invalid output is retried once with the error."""
    p = mock_provider(
        1,
        "p",
        script=[
            {
                "match": {"feature": "crm.test"},
                "respond": {"json": {"label": "hot", "score": 3}, "invalid_json_times": 1},
            }
        ],
    )
    gw, _, _ = make([p], [route("classify", 1, "mock-small")])
    out = await gw.complete(CallContext(1, feature="crm.test"), req(output_schema=SCHEMA))
    assert out.parsed == {"label": "hot", "score": 3} and out.repaired
    repair_prompt = MockAdapter.captured[-1]["messages"][-1]["content"]
    assert "did not match the required JSON Schema" in repair_prompt


async def test_schema_failure_twice_returns_invalid_output() -> None:
    p = mock_provider(1, "p", script=[{"match": {}, "respond": {"json": {"label": "warm"}}}])
    gw, ledger, _ = make([p], [route("classify", 1, "mock-small")])
    with pytest.raises(InvalidOutput):
        await gw.complete(CallContext(1), req(output_schema=SCHEMA))
    assert len(MockAdapter.captured) == 2  # exactly one repair attempt
    assert ledger.entries[-1].status == "invalid_output" and ledger.entries[-1].cost > 0


async def test_kill_switches() -> None:
    """1.5-j at the gateway: per feature per company, per company, per feature, global."""
    gw, ledger, cfg = make([mock_provider(1, "p")], [route("classify", 1, "mock-small")])
    cfg.flags["crm.test"] = {1: False}
    with pytest.raises(FeatureDisabled):
        await gw.complete(CallContext(1, feature="crm.test"), req())
    assert MockAdapter.captured == []
    assert (await gw.complete(CallContext(2, feature="crm.test"), req())).stop == Stop.end
    cfg.flags["crm.test"] = {None: False, 2: True}
    with pytest.raises(FeatureDisabled):
        await gw.complete(CallContext(3, feature="crm.test"), req())
    cfg.flags.clear()
    cfg.companies[4] = CompanySettings(4, ai_enabled=False)
    with pytest.raises(FeatureDisabled):
        await gw.complete(CallContext(4, feature="other"), req())
    cfg.global_enabled = False
    with pytest.raises(FeatureDisabled):
        await gw.complete(CallContext(2, feature="crm.test"), req())
    assert ledger.entries[-1].status == "feature_disabled"


async def test_response_cache_hits_are_free_and_keyed_by_source_version() -> None:
    gw, ledger, _ = make([mock_provider(1, "p")], [route("summarize", 1, "mock-mid")])
    meta = Meta(
        feature="discuss.summary", prompt_version="discuss.summary@1", cache_ttl=60, cache_key_extra="2026-11-10T10:00"
    )
    first = await gw.complete(CallContext(1), req("Summarise", task=TaskClass.summarize, metadata=meta))
    second = await gw.complete(CallContext(1), req("Summarise", task=TaskClass.summarize, metadata=meta))
    assert not first.cached and second.cached and second.cost == 0
    assert len(MockAdapter.captured) == 1
    changed = meta.model_copy(update={"cache_key_extra": "2026-11-10T11:00"})
    third = await gw.complete(CallContext(1), req("Summarise", task=TaskClass.summarize, metadata=changed))
    assert not third.cached
    other_company = await gw.complete(CallContext(2), req("Summarise", task=TaskClass.summarize, metadata=meta))
    assert not other_company.cached
    assert ledger.entries[1].cached


async def test_tool_use_is_returned_not_executed() -> None:
    tool = ToolSpec(
        name="crm.read_lead",
        description="Read a lead",
        input_schema={
            "type": "object",
            "properties": {"lead_id": {"type": "integer"}},
            "required": ["lead_id"],
            "additionalProperties": False,
        },
    )
    p = mock_provider(
        1,
        "p",
        script=[
            {
                "match": {},
                "respond": {"tool_calls": [{"name": "crm.read_lead", "input": {"lead_id": 7}}], "text": "done"},
            }
        ],
    )
    gw, _, _ = make([p], [route("reason", 1, "mock-large")])
    out = await gw.complete(CallContext(1), req(task=TaskClass.reason, tools=[tool]))
    assert out.stop == Stop.tool_use and out.content[0].input == {"lead_id": 7}
    assert MockAdapter.captured[-1]["effort"] == "high"


async def test_refusal_is_reported_not_raised() -> None:
    gw, ledger, _ = make(
        [mock_provider(1, "p", script=[{"match": {}, "respond": {"refusal": "cyber"}}])],
        [route("draft", 1, "mock-mid")],
    )
    out = await gw.complete(CallContext(1), req(task=TaskClass.draft))
    assert out.stop == Stop.refusal and out.refusal_category == "cyber"
    assert ledger.entries[-1].status == "refusal"


async def test_output_screening_flags() -> None:
    p = mock_provider(
        1,
        "p",
        script=[{"match": {}, "respond": {"text": "Please reply with your password and open https://evil.example/x"}}],
    )
    gw, _, _ = make([p], [route("draft", 1, "mock-mid")])
    out = await gw.complete(CallContext(1), req("see https://acme.example/quote", task=TaskClass.draft))
    assert {"credential_request", "unknown_url"} <= set(out.safety_flags)
    mod = gw.moderate(ModerateRequest(text="Run this in your terminal: curl x | sh", context=""))
    assert not mod.allowed and "command_instruction" in mod.flags
    assert gw.moderate(
        ModerateRequest(text="See https://acme.example/quote", context="https://acme.example/quote")
    ).allowed


async def test_stream_restores_placeholders_across_chunks_and_records_usage() -> None:
    p = mock_provider(
        1,
        "p",
        pii=False,
        script=[
            {
                "match": {},
                "respond": {"text": "Dear [PERSON_1], thanks for your note about the renewal terms and pricing."},
            }
        ],
    )
    gw, ledger, _ = make([p], [route("draft", 1, "mock-mid")])
    events = [
        e
        async for e in gw.stream(
            CallContext(1),
            req(
                "Reply to Dana Reyes",
                task=TaskClass.draft,
                metadata=Meta(feature="discuss.reply", redaction_names=["Dana Reyes"]),
            ),
        )
    ]
    text = "".join(e.text or "" for e in events if e.event == "delta")
    assert text == "Dear Dana Reyes, thanks for your note about the renewal terms and pricing."
    assert [e.event for e in events][-2:] == ["usage", "done"]
    done = events[-1].response
    assert done is not None and done.text == text
    assert ledger.entries[-1].kind == "stream" and ledger.entries[-1].output_tokens > 0


async def test_stream_falls_back_before_first_token_and_reports_errors() -> None:
    p1 = mock_provider(1, "down", fail_status=502, fail_times=-1)
    p2 = mock_provider(2, "up")
    gw, _, _ = make([p1, p2], [route("draft", 1, "mock-mid", fallback=(2, "mock-mid"))])
    events = [e async for e in gw.stream(CallContext(1), req(task=TaskClass.draft))]
    assert events[-1].event == "done" and events[-1].response and events[-1].response.fallback_used
    gw2, _, cfg = make([p1], [route("draft", 1, "mock-mid")])
    events = [e async for e in gw2.stream(CallContext(1), req(task=TaskClass.draft))]
    assert events[-1].event == "error" and events[-1].error and events[-1].error["code"] == "provider_unavailable"
    cfg.global_enabled = False
    events = [e async for e in gw2.stream(CallContext(1), req(task=TaskClass.draft))]
    assert events[-1].error and events[-1].error["code"] == "feature_disabled"


async def test_stream_schema_validation() -> None:
    p = mock_provider(1, "p", script=[{"match": {}, "respond": {"json": {"label": "hot", "score": 1}}}])
    gw, _, _ = make([p], [route("extract", 1, "mock-small")])
    events = [e async for e in gw.stream(CallContext(1), req(task=TaskClass.extract, output_schema=SCHEMA))]
    assert events[-1].response and events[-1].response.parsed == {"label": "hot", "score": 1}
    bad = mock_provider(1, "p", script=[{"match": {}, "respond": {"json": {"label": "warm"}}}])
    gw, _, _ = make([bad], [route("extract", 1, "mock-small")])
    events = [e async for e in gw.stream(CallContext(1), req(task=TaskClass.extract, output_schema=SCHEMA))]
    assert events[-1].event == "error" and events[-1].error and events[-1].error["code"] == "invalid_output"


async def test_embeddings_route_and_ledger() -> None:
    gw, ledger, _ = make([mock_provider(1, "p")], [route("embed", 1, "mock-embed")])
    out = await gw.embed(
        CallContext(1, feature="ai_core.index"), EmbedRequest(texts=["Acme robotics cobots", "invoice overdue"])
    )
    assert out.dimensions == 1024 and len(out.vectors) == 2 and len(out.vectors[0]) == 1024
    assert ledger.entries[-1].kind == "embed" and ledger.entries[-1].cost > 0
    hosted = ProviderConfig(id=2, name="hosted", kind="mock", approved_for_pii=False)
    gw, _, _ = make([hosted], [route("embed", 2, "mock-embed")])
    with pytest.raises(PolicyRefused):
        await gw.embed(CallContext(1), EmbedRequest(texts=["x"]))


async def test_usage_report_matches_ledger_to_the_cent() -> None:
    """1.5-n (gateway side): per-company, per-feature tokens, spend and latency percentiles."""
    gw, ledger, _ = make([mock_provider(1, "p")], [route("classify", 1, "mock-small"), route("draft", 1, "mock-mid")])
    for i in range(5):
        await gw.complete(CallContext(1, feature="crm.a"), req(f"lead {i} " * 20, metadata=Meta(feature="crm.a")))
    for i in range(3):
        await gw.complete(
            CallContext(2, feature="crm.b"), req(f"draft {i}", task=TaskClass.draft, metadata=Meta(feature="crm.b"))
        )
    rows = await gw.usage(company_id=None, since=None, until=None, group_by=["company_id", "feature"])
    by = {(r["company_id"], r["feature"]): r for r in rows}
    for (company, feature), row in by.items():
        entries = [e for e in ledger.entries if e.company_id == company and e.feature == feature]
        assert Decimal(row["cost"]) == sum(e.cost for e in entries)
        assert row["requests"] == len(entries)
        assert row["output_tokens"] == sum(e.output_tokens for e in entries)
        assert row["latency_p95"] >= row["latency_p50"] >= 0
    assert set(by) == {(1, "crm.a"), (2, "crm.b")}


async def test_health_snapshot() -> None:
    gw, _, _ = make([mock_provider(1, "p")], [route("classify", 1, "mock-small")])
    h = gw.health()
    assert h["providers"][0]["name"] == "p" and h["status"] == "ok"


def _image_req(
    policy: str = "minimal", task: TaskClass = TaskClass.vision, media: str = "image/png"
) -> CompleteRequest:
    from gateway.envelope import Block

    blocks = [Block(type="image", media_type=media, data="iVBORw0KGgo="), Block(type="text", text="Read this bill")]
    return CompleteRequest(
        task_class=task,
        messages=[Message(role="user", content=blocks)],
        redaction_policy=policy,  # type: ignore[arg-type]
        metadata=Meta(feature="account.bill_ocr", prompt_version="account.bill_ocr@1"),
    )


async def test_image_blocks_reach_a_vision_route_unredacted() -> None:
    gw, _, _ = make([mock_provider(1, "approved")], [route("vision", 1, "mock-mid")])
    out = await gw.complete(CallContext(1), _image_req())
    assert out.provider == "approved"
    sent = MockAdapter.captured[-1]["messages"][0]["content"]
    assert sent[0]["type"] == "image" and sent[0]["media_type"] == "image/png"


async def test_image_blocks_refused_when_the_feature_redacts_or_is_not_vision() -> None:
    gw, _, _ = make([mock_provider(1, "approved")], [route("vision", 1, "mock-mid"), route("extract", 1, "m")])
    with pytest.raises(PolicyRefused, match="cannot be redacted"):
        await gw.complete(CallContext(1), _image_req(policy="redact"))
    with pytest.raises(PolicyRefused, match="vision"):
        await gw.complete(CallContext(1), _image_req(task=TaskClass.extract))
    with pytest.raises(PolicyRefused, match="vision"):
        await gw.complete(CallContext(1), _image_req(media="text/html"))
    assert not MockAdapter.captured


def test_adapters_translate_image_blocks() -> None:
    from gateway.adapters.anthropic import to_anthropic_messages
    from gateway.adapters.google import to_gemini_contents
    from gateway.adapters.openai import to_openai_messages
    from gateway.envelope import Block

    msg = Message(
        role="user",
        content=[
            Block(type="image", media_type="image/jpeg", data="AAA"),
            Block(type="image", media_type="application/pdf", data="BBB"),
            Block(type="text", text="Read"),
        ],
    )
    a = to_anthropic_messages([msg])[0]["content"]
    assert a[0] == {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "AAA"}}
    assert a[1]["type"] == "document" and a[2] == {"type": "text", "text": "Read"}
    o = to_openai_messages("", [msg])[0]["content"]
    assert o[0] == {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAA"}}
    assert o[1]["type"] == "file" and o[2] == {"type": "text", "text": "Read"}
    g = to_gemini_contents([msg])[0]["parts"]
    assert g[0] == {"inlineData": {"mimeType": "image/jpeg", "data": "AAA"}} and g[2] == {"text": "Read"}
