"""Jobs of the workflow module."""

from __future__ import annotations

from typing import Any

import httpx

from tessera.jobs import job


@job("workflow.run_timed", max_attempts=1, as_system=True)
def run_timed_job(env: Any) -> dict[str, Any]:
    from modules.workflow.engine import run_time_automations

    return {"fired": run_time_automations(env)}


@job("workflow.call_webhook", queue="webhooks", max_attempts=6, backoff_seconds=60)
def call_webhook_job(env: Any, url: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(url, json=payload, timeout=10)
    resp.raise_for_status()  # non-2xx raises → retried with backoff
    return {"status": resp.status_code}
