"""Evaluation gates of the accounting AI features (2-i): every feature is complete (prompt, flag, thresholds,
≥ 100 cases with prompt injections, cassettes) and meets its thresholds in replay mode."""

from __future__ import annotations

from typing import Any

import pytest

from tessera.ai import evals as E

pytestmark = pytest.mark.db
FEATURES = {"account.bill_ocr", "account.anomaly", "account.cashflow_forecast"}


def _spec(name: str) -> E.FeatureSpec:
    return next(s for s in E.feature_specs(modules=["account"]) if s.name == name)


def test_features_are_complete() -> None:
    specs = E.feature_specs(modules=["account"])
    assert {s.name for s in specs} == FEATURES
    assert E.completeness(specs) == []
    for s in specs:
        assert int(s.eval["min_cases"]) >= 100 and len(s.cases()) >= 100
        assert s.data["flag"] == {"default": True, "rollout": "per_company"} and s.data["risk_class"]


@pytest.mark.parametrize("name", ["account.anomaly", "account.cashflow_forecast"])
def test_evaluation_passes_in_replay(env: Any, name: str) -> None:
    report = E.evaluate(env, _spec(name))
    assert report.passed, report.failures
    assert report.cases >= 100


def test_bill_reading_evaluation_passes_on_a_stratified_sample(env: Any) -> None:
    """The full 125-bill run is ``tessera ai eval run account.bill_ocr`` (CI); here every third bill plus every
    injection, isolation and customer-invoice case keeps the suite fast with every layout and language."""
    spec = _spec("account.bill_ocr")
    cases = [
        c
        for i, c in enumerate(spec.cases())
        if i % 3 == 0 or {"injection", "isolation", "out_invoice"} & set(c["tags"])
    ]
    spec.cases = lambda: cases  # type: ignore[method-assign]
    spec.data["eval"] = {**spec.data["eval"], "min_cases": len(cases)}
    report = E.evaluate(env, spec)
    assert report.passed, report.failures
    assert {lang for c in cases for lang in c["tags"] if lang in ("en", "fr", "de", "es")} == {"en", "fr", "de", "es"}
    assert report.metrics["proposal_gate"] == 1.0 and report.metrics["isolation"] == 1.0
