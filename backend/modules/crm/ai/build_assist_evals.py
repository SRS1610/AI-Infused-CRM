"""Builds the evaluation sets and replay cassettes of the CRM assist features.

Run: ``python -m modules.crm.ai.build_assist_evals`` (from ``backend/``). Output is deterministic.

The model answers below are **hand-authored** (no provider keys here): replaying them measures the
pipeline — schema handling, source building as the user, citation verification, access isolation,
proposals, the price-grounding check and the domain validator — not the quality of a model. They are
written to be realistic: mostly right, with a few typical slips (a missed job title, a boundary read as
"at least", a too-narrow "high priority") so the metrics are computed on something other than a perfect
copy of the reference. Re-record them against a real provider with ``tessera ai eval run <feature>
--mode record``; the expected values stay.

Cassette keys: ``<feature>@1 | <case> | <call index>``; rubric cases get the judge's answer
(``eval.judge@1``) at the next index.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from modules.crm.ai.assist_cases import calls, extract, followup, objections, search

HERE = Path(__file__).parent


def _write(name: str, cases: list[dict[str, Any]], tape: list[dict[str, Any]]) -> None:
    (HERE / "evals").mkdir(exist_ok=True)
    (HERE / "cassettes").mkdir(exist_ok=True)
    (HERE / "evals" / f"{name}.jsonl").write_text("\n".join(json.dumps(c, sort_keys=True) for c in cases) + "\n")
    tape = sorted(tape, key=lambda t: (t["case"], t["index"]))
    (HERE / "cassettes" / f"{name}.jsonl").write_text("\n".join(json.dumps(t, sort_keys=True) for t in tape) + "\n")


def build() -> None:
    for module in (extract, followup, calls, objections, search):
        name, cases, tape = module.build()
        assert len(cases) >= 30 and any("injection" in c.get("tags", []) for c in cases), name
        assert len({c["id"] for c in cases}) == len(cases), name
        _write(name, cases, tape)


if __name__ == "__main__":
    build()
