"""1.4-k: list of 100k leads < 300 ms p95; kanban grouped by stage with counts and sums < 500 ms p95."""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from typing import Any

import pytest

pytestmark = [pytest.mark.db, pytest.mark.perf]
ROWS = 100_000


def p95(fn: Callable[[], Any], runs: int = 15) -> tuple[float, float]:
    fn()  # warm the cache of query plans
    timings = []
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        timings.append(time.perf_counter() - started)
    timings.sort()
    return timings[int(len(timings) * 0.95) - 1], statistics.median(timings)


@pytest.fixture
def seeded(env: Any, env_for: Any) -> Any:
    stages = env["crm.stage"].search([]).ids
    users = env["core.user"].search([("login", "in", ["admin", "demo"])]).ids
    team = env.ref("crm.team_sales").ids[0]
    env.cr.execute(
        """INSERT INTO crm_lead (name, type, stage_id, user_id, team_id, expected_revenue, probability, prorated_revenue,
                                 priority, active, company_id, currency_id, create_uid, write_uid, create_date, write_date)
           SELECT 'perf lead ' || g, 'opportunity', (:stages)[1 + g % array_length(:stages, 1)], (:users)[1 + g % 2], :team,
                  (g % 500) * 100, 10 + g % 80, (g % 500) * (10 + g % 80), (g % 4)::text, true, 1, 1, 2, 2, now(), now()
             FROM generate_series(1, :n) AS g""",
        {"stages": stages, "users": users, "team": team, "n": ROWS},
    )
    env.cr.execute("ANALYZE crm_lead")
    env.cache.invalidate()
    return env_for("demo")  # a CRM user: record rules and the multi-company rule apply


def test_list_and_kanban_on_100k_leads(seeded: Any) -> None:
    leads = seeded["crm.lead"]
    fields = ["name", "partner_id", "stage_id", "user_id", "expected_revenue", "date_deadline"]

    def list_page() -> None:
        leads.search_read([("type", "=", "opportunity")], fields, limit=80)
        leads.search_count([("type", "=", "opportunity")])

    def kanban() -> None:
        groups = leads.read_group([("type", "=", "opportunity")], ["expected_revenue:sum"], ["stage_id"])
        for g in groups:  # first cards of every column
            leads.search_read(
                g["__domain"], ["name", "partner_id", "expected_revenue", "priority", "user_id"], limit=20
            )

    list_p95, list_median = p95(list_page)
    kanban_p95, kanban_median = p95(kanban)
    groups = leads.read_group([("type", "=", "opportunity")], ["expected_revenue:sum"], ["stage_id"])
    assert sum(g["__count"] for g in groups) >= ROWS
    assert list_p95 < 0.3, f"list p95 {list_p95 * 1000:.0f} ms (median {list_median * 1000:.0f})"
    assert kanban_p95 < 0.5, f"kanban p95 {kanban_p95 * 1000:.0f} ms (median {kanban_median * 1000:.0f})"
