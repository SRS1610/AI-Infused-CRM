"""CRM assist evaluation sets in replay (1.6-a / 1.6-b): every threshold met, nothing missing for --strict."""

from __future__ import annotations

from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from modules.crm.ai_assist import FEATURES
from tessera.ai import client as C
from tessera.ai import evals as E

pytestmark = pytest.mark.db


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def test_strict_report_has_nothing_missing_for_assist_features() -> None:
    specs = E.feature_specs(list(FEATURES))
    assert sorted(s.name for s in specs) == sorted(FEATURES)
    assert E.completeness(specs) == []
    for spec in specs:
        cases = spec.cases()
        assert len(cases) >= 30 and sum("injection" in c.get("tags", []) for c in cases) >= 2, spec.name
        assert spec.data["flag"] == {"default": True, "rollout": "per_company"}


@pytest.mark.parametrize("feature", FEATURES)
def test_replay_meets_thresholds(env: Any, feature: str) -> None:
    (spec,) = E.feature_specs([feature])
    report = E.evaluate(env, spec)
    failing = [
        {k: r.get(k) for k in ("case", "status", "error", "flags", "unexpected_writes", "proposals")}
        for r in report.case_results
        if r["status"] != "ok" or r["unexpected_writes"]
    ]
    assert report.passed, (report.failures, report.metrics, failing[:5])
    assert not failing, failing[:5]
    assert all(r["unexpected_writes"] == 0 for r in report.case_results)
