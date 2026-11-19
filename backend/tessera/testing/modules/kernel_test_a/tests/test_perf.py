"""1.3-l: pivot read_group on 100k rows, two row groups and one column group, p95 < 500 ms."""

from __future__ import annotations

import statistics
import time
from typing import Any

import pytest

pytestmark = [pytest.mark.db, pytest.mark.perf]

ROWS = 100_000


@pytest.fixture
def seeded(env: Any) -> Any:
    authors = env["ktest.author"].create([{"name": f"Perf author {i}"} for i in range(50)])
    ids = authors.ids
    env.cr.execute(
        """INSERT INTO ktest_book (name, status, author_id, page_count, price, published, active, company_id,
                                   create_uid, write_uid, create_date, write_date)
           SELECT 'perf ' || g,
                  (ARRAY['draft','review','published'])[1 + g % 3],
                  (:ids)[1 + g % 50],
                  50 + g % 700,
                  (g % 90) + 0.99,
                  DATE '2019-01-01' + (g % 2500),
                  true, 1, 2, 2, now(), now()
             FROM generate_series(1, :n) AS g""",
        {"ids": ids, "n": ROWS},
    )
    env.cr.execute("ANALYZE ktest_book")
    env.cache.invalidate()
    return env


def test_pivot_read_group_p95(seeded: Any) -> None:
    books = seeded["ktest.book"]
    domain = [("name", "like", "perf %")]
    # pivot: rows = status x author, columns = year; one query per cell set, as the client issues it
    timings = []
    result: list[dict[str, Any]] = []
    for _ in range(12):
        started = time.perf_counter()
        result = books.read_group(domain, ["page_count:sum", "price:avg"], ["status", "author_id", "published:year"])
        timings.append(time.perf_counter() - started)
    timings.sort()
    p95 = timings[int(len(timings) * 0.95) - 1]
    assert sum(g["__count"] for g in result) == ROWS
    assert len(result) == 3 * 50 * 7  # 3 statuses x 50 authors x 7 years (2019-2025)
    assert all(
        isinstance(g["author_id"], dict) and g["author_id"]["display_name"].startswith("Perf author") for g in result
    )
    assert p95 < 0.5, f"p95 {p95 * 1000:.0f} ms (median {statistics.median(timings) * 1000:.0f} ms)"
    # the graph view asks for the same aggregates with one group-by: totals must match
    by_status = books.read_group(domain, ["page_count:sum"], ["status"])
    assert sum(g["page_count:sum"] for g in by_status) == pytest.approx(sum(g["page_count:sum"] for g in result))
