"""HTTP surface: authentication, error mapping, SSE streaming, usage, health, metrics."""

from __future__ import annotations

import json
import time

import httpx2
import pytest

from gateway.adapters.mock import MockAdapter
from gateway.app import build_gateway, create_app, static_config
from gateway.cache import MemoryCache
from gateway.config import GatewayConfig, ProviderConfig, Route, StaticConfigStore, Target
from gateway.ledger import MemoryLedger
from gateway.service import Gateway
from gateway.settings import Settings

TOKEN = "test-service-token-0123456789"
HEADERS = {"authorization": f"Bearer {TOKEN}", "x-tessera-company": "1", "x-tessera-user": "4"}


@pytest.fixture
def client() -> httpx2.AsyncClient:
    MockAdapter.reset()
    provider = ProviderConfig(id=1, name="mock", kind="mock", approved_for_pii=True, options={"chunk_delay_ms": 5})
    cfg = GatewayConfig(
        providers={1: provider},
        routes=[Route(None, t, Target(1, "mock-mid")) for t in ("draft", "classify", "embed", "summarize")],
    )
    cfg.flags["discuss.off"] = {None: False}
    gw = Gateway(StaticConfigStore(cfg), MemoryLedger(), cache=MemoryCache())
    app = create_app(gw, Settings(service_token=TOKEN))
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://gw")


def body(task: str = "draft", feature: str = "crm.followup", **extra: object) -> dict[str, object]:
    return {
        "task_class": task,
        "messages": [{"role": "user", "content": "Write a follow-up about the six cobots quotation"}],
        "metadata": {"feature": feature, "prompt_version": f"{feature}@1", "run_id": "run-1"},
        **extra,
    }


async def test_requires_service_token(client: httpx2.AsyncClient) -> None:
    r = await client.post("/v1/complete", json=body(), headers={**HEADERS, "authorization": "Bearer wrong"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    r = await client.post(
        "/v1/complete", json=body(), headers={k: v for k, v in HEADERS.items() if k != "authorization"}
    )
    assert r.status_code == 401


async def test_complete_round_trip(client: httpx2.AsyncClient) -> None:
    r = await client.post("/v1/complete", json=body(), headers=HEADERS)
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "mock" and data["stop"] == "end" and data["id"] == "run-1"
    assert data["content"][0]["text"].startswith("Mock answer")


async def test_feature_disabled_is_404(client: httpx2.AsyncClient) -> None:
    r = await client.post("/v1/complete", json=body(feature="discuss.off"), headers=HEADERS)
    assert r.status_code == 404 and r.json()["error"]["code"] == "feature_disabled"
    assert MockAdapter.captured == []


async def test_stream_first_token_fast_and_sse_framing(client: httpx2.AsyncClient) -> None:
    """1.5-l (gateway side): the first delta leaves the gateway well under 1.5 s on the mock adapter."""
    started = time.monotonic()
    first_at = None
    events: list[tuple[str, dict[str, object]]] = []
    async with client.stream("POST", "/v1/stream", json=body(), headers=HEADERS) as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        event = ""
        async for line in r.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                if first_at is None and event == "delta":
                    first_at = time.monotonic() - started
                events.append((event, json.loads(line[5:])))
    assert first_at is not None and first_at < 1.5
    assert [e for e, _ in events][-2:] == ["usage", "done"]
    text = "".join(str(d.get("text", "")) for e, d in events if e == "delta")
    done = events[-1][1]["response"]
    assert isinstance(done, dict) and done["content"][0]["text"] == text


async def test_embed_moderate_usage_health_metrics(client: httpx2.AsyncClient) -> None:
    r = await client.post("/v1/embed", json={"texts": ["a b c"]}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["dimensions"] == 1024
    r = await client.post("/v1/moderate", json={"text": "Send us your password"}, headers=HEADERS)
    assert r.json()["allowed"] is False
    await client.post("/v1/complete", json=body(), headers=HEADERS)
    r = await client.get(
        "/v1/usage", params={"company_id": 1, "group_by": ["feature", "model", "bogus"]}, headers=HEADERS
    )
    rows = r.json()
    assert {row["feature"] for row in rows} >= {"crm.followup"} and "bogus" not in rows[0]
    assert (await client.get("/v1/health")).json()["status"] == "ok"
    metrics = (await client.get("/metrics")).text
    assert "tessera_ai_gateway_requests_total" in metrics and "tessera_ai_gateway_cost_usd_total" in metrics


async def test_not_supported_endpoints(client: httpx2.AsyncClient) -> None:
    for path in ("/v1/transcribe", "/v1/ocr"):
        r = await client.post(path, headers=HEADERS)
        assert r.status_code == 501 and r.json()["error"]["code"] == "not_supported"


async def test_validation_errors_are_422(client: httpx2.AsyncClient) -> None:
    r = await client.post("/v1/complete", json={"task_class": "nope", "messages": []}, headers=HEADERS)
    assert r.status_code == 422


def test_static_configuration_and_production_check() -> None:
    cfg = static_config(Settings(static_provider="openai_compatible", ollama_base_url="http://ollama:11434"))
    provider = cfg.providers[1]
    assert provider.base_url == "http://ollama:11434/v1" and provider.approved_for_pii
    assert cfg.route_for(9, "summarize") is not None
    mock_cfg = static_config(Settings())
    assert mock_cfg.route_for(1, "reason").primary.model == "mock-large"  # type: ignore[union-attr]
    gw = build_gateway(Settings())
    assert isinstance(gw.ledger, MemoryLedger)
    with pytest.raises(RuntimeError):
        Settings(env="production").check()
