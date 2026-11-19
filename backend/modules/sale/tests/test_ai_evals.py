"""Evaluation gate of the sales AI features (replay): complete declarations, thresholds met, no mutation."""

from __future__ import annotations

from typing import Any

import pytest

from tessera.ai import evals as EV

pytestmark = pytest.mark.db
FEATURES = {"sale.upsell", "sale.discount_suggest", "sale.quote_from_text", "sale.pricing_breach"}


def test_sale_features_are_complete() -> None:
    specs = EV.feature_specs(modules=["sale"])
    assert {s.name for s in specs} == FEATURES
    assert EV.completeness(specs) == []


@pytest.mark.parametrize("name", sorted(FEATURES))
def test_sale_evaluations_pass_in_replay(env: Any, name: str) -> None:
    (spec,) = EV.feature_specs([name], ["sale"])
    report = EV.evaluate(env, spec)
    print(name, report.cases, report.metrics, report.failures)
    assert report.passed, (name, report.failures, report.metrics)
    assert all(r["unexpected_writes"] == 0 for r in report.case_results), name
    assert all(r["status"] == "ok" for r in report.case_results), [
        (r["case"], r.get("error")) for r in report.case_results if r["status"] != "ok"
    ]
