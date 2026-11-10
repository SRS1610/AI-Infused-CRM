"""`tessera ai provider` and `tessera perf` helpers."""

from __future__ import annotations

from typing import Any

import pytest

from tessera.cli.ai import TIERS, configure_provider
from tessera.cli.perf import _p, clone_rows

pytestmark = pytest.mark.db


def test_configure_provider_routes_the_default_tiers_and_is_idempotent(env: Any) -> None:
    sys_env = env.elevated("test")
    company_route = sys_env["ai_core.route"].create(
        {
            "company_id": env.company.ids[0],
            "task_class": "draft",
            "provider_id": sys_env["ai_core.provider"].search([], limit=1).ids[0],
            "model": "keep-me",
        }
    )
    label, routed = configure_provider(sys_env, "anthropic", region="eu")
    assert label == "Anthropic" and dict(routed) == TIERS["anthropic"]
    provider = sys_env["ai_core.provider"].search([("name", "=", "Anthropic")])
    assert len(provider) == 1 and provider.region == "eu" and not provider.api_key  # key from the environment
    for task_class, model in TIERS["anthropic"].items():
        route = sys_env["ai_core.route"].search([("company_id", "=", None), ("task_class", "=", task_class)])
        assert route.provider_id.ids == provider.ids and route.model == model
    assert company_route.model == "keep-me"  # company routes are left alone
    configure_provider(sys_env, "anthropic", region="eu")
    assert len(sys_env["ai_core.provider"].search([("name", "=", "Anthropic")])) == 1


def test_clone_rows_fills_a_table_and_keeps_unique_columns_unique(cr: Any) -> None:
    cr.execute("CREATE TABLE perf_probe (id serial PRIMARY KEY, code varchar UNIQUE, qty int)")
    cr.execute("INSERT INTO perf_probe (code, qty) VALUES ('A', 1), ('B', 2), ('C', 3)")
    assert clone_rows(cr, "perf_probe", 10) == 7
    assert cr.scalar("SELECT count(*) FROM perf_probe") == 10
    assert cr.scalar("SELECT count(DISTINCT code) FROM perf_probe") == 10
    assert clone_rows(cr, "perf_probe", 5) == 0  # already above the target


def test_percentile() -> None:
    assert _p([5.0, 1.0, 3.0, 2.0, 4.0], 0.5) == 3.0
    assert _p([float(i) for i in range(1, 101)], 0.95) == 95.0
