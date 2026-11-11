"""``account_bank.reconcile_suggest`` evaluation gate in replay: complete for ``--strict``, thresholds met."""

from __future__ import annotations

from typing import Any

import pytest
from gateway.adapters.mock import MockAdapter

from tessera.ai import client as C
from tessera.ai import evals as E

pytestmark = pytest.mark.db
FEATURE = "account_bank.reconcile_suggest"


@pytest.fixture(autouse=True)
def _fresh() -> Any:
    MockAdapter.reset()
    C.reset_inprocess_state()
    yield
    C.set_client_factory(None)


def test_strict_report_has_nothing_missing() -> None:
    specs = E.feature_specs([FEATURE])
    assert E.completeness(specs) == []
    cases = specs[0].cases()
    assert len(cases) >= 100 and sum("injection" in c["tags"] for c in cases) >= 5


def test_replay_meets_thresholds(env: Any) -> None:
    (spec,) = E.feature_specs([FEATURE])
    report = E.evaluate(env, spec)
    wrong = [
        (r["case"], r["_case"]["tags"] if "_case" in r else None, r.get("error"), (r.get("data") or {}).get("label"))
        for r in report.case_results
        if r["status"] != "ok"
    ]
    assert report.passed, (report.failures, report.metrics, wrong[:5])
    assert report.metrics["top1_accuracy"] >= 0.85
    assert all(r["unexpected_writes"] == 0 for r in report.case_results)
