"""U-10: the API exposes Prometheus metrics for requests, jobs and AI feature runs."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tessera.config import get_settings
from tessera.metrics import exposition, record_ai_run


def test_metrics_endpoint_reports_requests_and_ai_runs(client: TestClient) -> None:
    client.get("/api/v1/auth/session")
    record_ai_run("test.feature", "ok", {"input_tokens": 120, "output_tokens": 30, "cost": 0.002, "latency_ms": 450})
    record_ai_run("test.feature", "feature_disabled")
    body = client.get("/metrics").text
    assert 'tessera_http_requests_total{method="GET",route="/api/v1/auth/session",status="4xx"}' in body
    assert 'tessera_ai_runs_total{feature="test.feature",status="ok"}' in body
    assert 'tessera_ai_runs_total{feature="test.feature",status="feature_disabled"}' in body
    assert 'tessera_ai_tokens_total{direction="input",feature="test.feature"}' in body
    assert "tessera_ai_cost_usd_total" in body and "tessera_ai_latency_seconds_bucket" in body
    assert "tessera_ai_gateway_requests" in body  # the in-process gateway's own metrics


def test_metrics_need_the_token_when_one_is_set_and_are_off_in_production_without(
    client: TestClient, monkeypatch: Any
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "metrics_token", "s3cret")
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/metrics", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    monkeypatch.setattr(settings, "metrics_token", "")
    monkeypatch.setattr(type(settings), "is_production", property(lambda self: True))
    assert client.get("/metrics").status_code == 404


def test_route_labels_use_templates_not_raw_paths(client: TestClient) -> None:
    client.get("/api/v1/models/core.user/123456")
    body = exposition().decode()
    assert "/api/v1/models/core.user/123456" not in body
    assert 'route="/api/v1/models/{model_name}/{record_id}"' in body
