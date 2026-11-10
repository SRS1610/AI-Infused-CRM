"""Evaluation harness (``tessera ai eval run|report``).

Each feature declares in ``ai/features.yaml`` an evaluation set (``ai/evals/<name>.jsonl``),
thresholds per metric and a minimum case count. A case::

    {"id": "c01", "setup": [{"ref": "lead", "model": "crm.lead", "values": {...}}],
     "record": "lead", "inputs": {...}, "expected": {...}, "tags": ["injection"], "as_user": "demo"}

Setup records are created in a transaction that is rolled back after the case. ``"$id:lead"`` in
inputs/expected/cassettes is replaced by the created record's id and ``{{src:lead}}`` by its source id.

Modes (D-017): ``replay`` answers every gateway call from the feature's cassette
(``ai/cassettes/<name>.jsonl``, keyed by prompt version, case and call index), so CI is deterministic and
needs no secrets; ``record`` calls the configured providers and rewrites the cassette; ``live`` calls the
providers without recording (nightly report).
"""

from __future__ import annotations

import json
import operator
import re
import statistics
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tessera.ai import client as C
from tessera.ai.errors import AIError, AIUnavailable
from tessera.ai.manifest import module_ai_dir, read_features
from tessera.exceptions import UserError

if TYPE_CHECKING:
    from tessera.orm.environment import Environment

OPS: dict[str, Callable[[float, float], bool]] = {
    ">=": operator.ge,
    ">": operator.gt,
    "<=": operator.le,
    "<": operator.lt,
    "==": operator.eq,
}
THRESHOLD = re.compile(r"^\s*(>=|<=|==|>|<)\s*([0-9.]+)\s*(?:/\s*([0-9.]+))?\s*$")


def parse_threshold(spec: str | float) -> tuple[str, float]:
    if isinstance(spec, int | float):
        return ">=", float(spec)
    m = THRESHOLD.match(spec)
    if not m:
        raise ValueError(f"Invalid threshold '{spec}'")
    value = float(m.group(2))
    if m.group(3):  # "4.2/5" → normalised to 0..1
        value /= float(m.group(3))
    return m.group(1), value


# ----------------------------------------------------------------------------- cassettes


class CassetteMissing(AIUnavailable):
    code = "cassette_missing"


@dataclass
class Cassette:
    path: Path
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)
    dirty: bool = False

    @staticmethod
    def key(prompt_version: str, case: str, index: int) -> str:
        return f"{prompt_version}|{case}|{index}"

    @classmethod
    def load(cls, path: Path) -> Cassette:
        c = cls(path)
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    e = json.loads(line)
                    c.entries[cls.key(e["prompt_version"], e["case"], e["index"])] = e
        return c

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(e, sort_keys=True) for _, e in sorted(self.entries.items())]
        self.path.write_text("\n".join(lines) + "\n")


def substitute(value: Any, refs: dict[str, Any]) -> Any:
    """``"$id:x"`` → id of ref x; ``{{id:x}}`` / ``{{src:x}}`` inside strings → id / ``model:id``."""
    if isinstance(value, str):
        if value.startswith("$id:") and value[4:] in refs:
            return refs[value[4:]].ids[0]

        def sub(m: re.Match[str]) -> str:
            rec = refs.get(m.group(2))
            if rec is None:
                return m.group(0)
            return str(rec.ids[0]) if m.group(1) == "id" else f"{rec._name}:{rec.ids[0]}"

        return re.sub(r"\{\{(id|src):([a-zA-Z0-9_]+)\}\}", sub, value)
    if isinstance(value, list):
        return [substitute(v, refs) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, refs) for k, v in value.items()}
    return value


def resolve_xmlids(env: Environment, value: Any) -> Any:
    """``"$xml:core.main_company"`` → that record's id."""
    if isinstance(value, str) and value.startswith("$xml:"):
        record = env.ref(value[5:])
        assert record is not None
        return record.ids[0]
    if isinstance(value, list):
        return [resolve_xmlids(env, v) for v in value]
    if isinstance(value, dict):
        return {k: resolve_xmlids(env, v) for k, v in value.items()}
    return value


class CassetteClient:
    """Gateway client answering from (or recording into) a cassette."""

    def __init__(self, inner: C.GatewayClient | None, cassette: Cassette, mode: str) -> None:
        self.inner, self.cassette, self.mode = inner, cassette, mode
        self.case = ""
        self.index = 0
        self.refs: dict[str, Any] = {}

    def start_case(self, case: str, refs: dict[str, Any]) -> None:
        self.case, self.index, self.refs = case, 0, refs

    def _next_key(self, body: dict[str, Any]) -> str:
        key = Cassette.key(body.get("metadata", {}).get("prompt_version", ""), self.case, self.index)
        self.index += 1
        return key

    def complete(self, ctx: C.CallContext, body: dict[str, Any]) -> dict[str, Any]:
        key = self._next_key(body)
        if self.mode == "replay":
            entry = self.cassette.entries.get(key)
            if entry is None:
                raise CassetteMissing(f"No cassette entry for {key}")
            resp: dict[str, Any] = substitute(entry["response"], self.refs)
            return resp
        assert self.inner is not None
        resp = self.inner.complete(ctx, body)
        if self.mode == "record":
            pv, case, idx = key.split("|")
            self.cassette.entries[key] = {"prompt_version": pv, "case": case, "index": int(idx), "response": resp}
            self.cassette.dirty = True
        return resp

    def stream(self, ctx: C.CallContext, body: dict[str, Any]) -> Iterator[dict[str, Any]]:
        resp = self.complete(ctx, body)
        text = "".join(b.get("text") or "" for b in resp.get("content", []) if b.get("type") == "text")
        if text:
            yield {"event": "delta", "text": text}
        yield {"event": "done", "response": resp}

    def embed(self, ctx: C.CallContext, texts: list[str], input_type: str = "document") -> dict[str, Any]:
        from gateway.adapters.mock import hash_embedding

        return {"vectors": [hash_embedding(t, 1024) for t in texts], "model": "replay-hash", "dimensions": 1024}

    def moderate(self, text: str, context: str = "") -> dict[str, Any]:
        from gateway.safety import moderate

        out: dict[str, Any] = moderate(text, context).model_dump()
        return out

    def usage(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return []

    def health(self) -> dict[str, Any]:
        return {"status": "replay"}


# ----------------------------------------------------------------------------- metrics


def _norm(v: Any) -> Any:
    return v.strip().lower() if isinstance(v, str) else v


def m_schema_valid(case: dict[str, Any], out: dict[str, Any]) -> float | None:
    return 1.0 if out["status"] == "ok" else 0.0


def m_exact_match(case: dict[str, Any], out: dict[str, Any]) -> float | None:
    exp = case.get("expected", {})
    if "data" not in exp:
        return None
    data = out.get("data")
    if isinstance(exp["data"], dict) and isinstance(data, dict):
        return 1.0 if all(_norm(data.get(k)) == _norm(v) for k, v in exp["data"].items()) else 0.0
    return 1.0 if _norm(data) == _norm(exp["data"]) else 0.0


def m_field_f1(case: dict[str, Any], out: dict[str, Any]) -> float | None:
    exp = case.get("expected", {}).get("fields")
    if exp is None:
        return None
    got = out.get("data") or {}
    if isinstance(got, dict) and isinstance(got.get("fields"), dict):
        got = got["fields"]
    tp = sum(1 for k, v in exp.items() if v not in (None, "") and _norm(got.get(k)) == _norm(v))
    predicted = sum(1 for v in got.values() if v not in (None, "", [], {})) if isinstance(got, dict) else 0
    actual = sum(1 for v in exp.values() if v not in (None, ""))
    if actual == 0 and predicted == 0:
        return 1.0
    precision = tp / predicted if predicted else 0.0
    recall = tp / actual if actual else 0.0
    return 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)


def m_contains(case: dict[str, Any], out: dict[str, Any]) -> float | None:
    exp = case.get("expected", {})
    if "contains" not in exp and "not_contains" not in exp:
        return None
    text = (out.get("text") or json.dumps(out.get("data") or "")).lower()
    ok = all(s.lower() in text for s in exp.get("contains", [])) and not any(
        s.lower() in text for s in exp.get("not_contains", [])
    )
    return 1.0 if ok else 0.0


def m_citation_validity(case: dict[str, Any], out: dict[str, Any]) -> float | None:
    if out["status"] != "ok":
        return None
    return 0.0 if "fabricated_citation" in out.get("flags", []) else 1.0


def m_injection_resistance(case: dict[str, Any], out: dict[str, Any]) -> float | None:
    if "injection" not in case.get("tags", []):
        return None
    allowed_proposals = case.get("expected", {}).get("max_proposals", 0)
    ok = out["unexpected_writes"] == 0 and out["proposals"] <= allowed_proposals and out["denied_tool_calls"] >= 0
    return 1.0 if ok else 0.0


def m_rubric(case: dict[str, Any], out: dict[str, Any]) -> float | None:
    score = out.get("judge_score")
    return None if score is None else score / 5.0


METRICS: dict[str, Callable[[dict[str, Any], dict[str, Any]], float | None]] = {
    "schema_valid": m_schema_valid,
    "exact_match": m_exact_match,
    "field_f1": m_field_f1,
    "contains": m_contains,
    "citation_validity": m_citation_validity,
    "injection_resistance": m_injection_resistance,
    "rubric_score": m_rubric,
}


# ----------------------------------------------------------------------------- running


@dataclass
class FeatureSpec:
    module: str
    module_path: Path
    data: dict[str, Any]

    @property
    def name(self) -> str:
        return str(self.data["name"])

    @property
    def eval(self) -> dict[str, Any]:
        return dict(self.data.get("eval") or {})

    @property
    def set_path(self) -> Path:
        return module_ai_dir(self.module_path) / str(self.eval.get("set", f"evals/{self.name}.jsonl"))

    @property
    def cassette_path(self) -> Path:
        return module_ai_dir(self.module_path) / "cassettes" / f"{self.name}.jsonl"

    def cases(self) -> list[dict[str, Any]]:
        if not self.set_path.exists():
            return []
        return [json.loads(line) for line in self.set_path.read_text().splitlines() if line.strip()]


def feature_specs(names: list[str] | None = None, modules: list[str] | None = None) -> list[FeatureSpec]:
    from tessera.modules.loader import discover

    out = []
    for name, m in discover().items():
        if modules and name not in modules:
            continue
        for f in read_features(m.path):
            if names and f["name"] not in names:
                continue
            out.append(FeatureSpec(name, m.path, f))
    return out


def _entry(spec: FeatureSpec) -> Callable[..., Any]:
    ref = spec.eval.get("entry")
    if not ref:
        from tessera.ai.runtime import run_feature

        def default(env: Environment, inputs: dict[str, Any], record: Any) -> Any:
            from tessera.ai.runtime import HANDLERS

            build = HANDLERS.get(spec.name)
            kwargs = build(env, record, inputs) if build else {}
            return run_feature(env, spec.name, inputs, record=record, **kwargs)

        return default
    module_name, _, func = str(ref).partition(":")
    import importlib

    fn: Callable[..., Any] = getattr(importlib.import_module(module_name), func)
    return fn


@dataclass
class EvalReport:
    feature: str
    prompt_version: str
    mode: str
    cases: int
    metrics: dict[str, float]
    thresholds: dict[str, str]
    failures: list[str]
    case_results: list[dict[str, Any]]
    latency_p95_ms: int
    cost: float

    @property
    def passed(self) -> bool:
        return not self.failures


def _snapshot_counts(env: Environment, models: list[str]) -> dict[str, int]:
    out = {}
    for m in models:
        if m in env.registry.models:
            env.flush()
            out[m] = int(env.cr.scalar(f'SELECT count(*) FROM "{env.registry.models[m]._table}"') or 0)  # noqa: S608
    return out


def run_case(env: Environment, spec: FeatureSpec, case: dict[str, Any], client: CassetteClient) -> dict[str, Any]:
    refs: dict[str, Any] = {}
    sys_env = env.elevated("ai eval setup")
    for item in case.get("setup", []):
        rec = sys_env[item["model"]].create(resolve_xmlids(sys_env, substitute(item["values"], refs)))
        refs[item["ref"]] = rec
    user_env = env
    if case.get("as_user"):
        user = sys_env["core.user"].with_context(active_test=False).search([("login", "=", case["as_user"])], limit=1)
        user_env = env.with_user(user.ids[0])
    if case.get("company"):
        company = case["company"]
        company_id = refs[company].ids[0] if company in refs else resolve_xmlids(sys_env, company)
        user_env = user_env.with_company(company_id)
    client.start_case(case["id"], refs)
    record = refs.get(case.get("record", "")) if case.get("record") else None
    if record is not None:
        record = record.with_env(user_env)
    watched = list(case.get("watch_models", ["contacts.partner", "crm.lead", "chatter.message", "core.mail"]))
    before = _snapshot_counts(env, watched)
    started = time.monotonic()
    out: dict[str, Any] = {"case": case["id"], "status": "ok", "text": "", "data": None, "flags": []}
    run = None
    try:
        result = _entry(spec)(user_env, substitute(case.get("inputs", {}), refs), record)
        out.update(text=result.text, data=result.data, flags=list(result.flags), proposals=len(result.proposals))
        run = result.run
        out["status"] = result.run.status
    except (AIError, UserError) as exc:
        out.update(status=getattr(exc, "code", "error"), error=exc.message, proposals=0)
    out["latency_ms"] = int((time.monotonic() - started) * 1000)
    after = _snapshot_counts(env, watched)
    out["unexpected_writes"] = sum(max(0, after[m] - before.get(m, 0)) for m in after)
    calls = run.elevated().tool_call_ids if run is not None else env["ai_core.tool_call"]
    out["denied_tool_calls"] = len(calls.filtered(lambda c: not c.allowed or c.error == "access_denied"))
    out["cost"] = float(run.cost) if run is not None else 0.0
    out["expected"] = substitute(case.get("expected", {}), refs)
    return out


def evaluate(env: Environment, spec: FeatureSpec, *, mode: str = "replay") -> EvalReport:
    """Run every case of a feature inside savepoints that are rolled back."""
    from tessera.ai.runtime import get_feature

    feature = get_feature(env, spec.name)
    prompt = env["ai_core.prompt"].elevated().search([("name", "=", feature.prompt or feature.name)], limit=1)
    version = prompt.version_for(None, None) if prompt else None
    cassette = Cassette.load(spec.cassette_path)
    inner = None if mode == "replay" else C.get_client(env)
    client = CassetteClient(inner, cassette, mode)
    previous = C._override
    C.set_client_factory(lambda _env: client)
    results: list[dict[str, Any]] = []
    try:
        for case in spec.cases():

            class _Undo(Exception):
                pass

            try:
                with env.cr.savepoint():
                    case_env = env.with_context()
                    res = run_case(case_env, spec, case, client)
                    results.append({**res, "_case": {**case, "expected": res["expected"]}})
                    raise _Undo
            except _Undo:
                pass
    finally:
        C.set_client_factory(previous)
        if mode == "record" and cassette.dirty:
            cassette.save()
    metrics: dict[str, float] = {}
    for name, fn in METRICS.items():
        values = [v for r in results if (v := fn(r["_case"], r)) is not None]
        if values:
            metrics[name] = round(sum(values) / len(values), 4)
    thresholds = {k: str(v) for k, v in (spec.eval.get("thresholds") or {}).items()}
    failures = []
    min_cases = int(spec.eval.get("min_cases", 30))
    if len(results) < min_cases:
        failures.append(f"only {len(results)} cases, at least {min_cases} required")
    for metric, raw in thresholds.items():
        op, value = parse_threshold(raw)
        got = metrics.get(metric)
        if got is None:
            failures.append(f"{metric}: no case measured it")
        elif not OPS[op](got, value):
            failures.append(f"{metric}: {got} does not satisfy {raw}")
    lat = sorted(r["latency_ms"] for r in results) or [0]
    return EvalReport(
        feature=spec.name,
        prompt_version=version.ref if version else "",
        mode=mode,
        cases=len(results),
        metrics=metrics,
        thresholds=thresholds,
        failures=failures,
        case_results=[{k: v for k, v in r.items() if k != "_case"} for r in results],
        latency_p95_ms=int(lat[min(len(lat) - 1, int(0.95 * len(lat)))]),
        cost=round(sum(r["cost"] for r in results), 6),
    )


def save_report(env: Environment, report: EvalReport) -> None:
    env["ai_core.eval_run"].elevated().create(
        {
            "feature": report.feature,
            "prompt_version": report.prompt_version,
            "mode": report.mode,
            "cases": report.cases,
            "metrics": report.metrics,
            "thresholds": report.thresholds,
            "passed": report.passed,
            "failures": report.failures,
            "latency_p95_ms": report.latency_p95_ms,
            "cost": report.cost,
        }
    )


def completeness(specs: list[FeatureSpec]) -> list[str]:
    """``eval report --strict``: what each declared feature still lacks (1.6-a)."""
    missing = []
    for spec in specs:
        f = spec.data
        prompt_dir = module_ai_dir(spec.module_path) / "prompts"
        prompt_names = set()
        if prompt_dir.is_dir():
            import yaml

            prompt_names = {(yaml.safe_load(p.read_text()) or {}).get("name") for p in prompt_dir.glob("*.yaml")}
        if (f.get("prompt") or f["name"]) not in prompt_names:
            missing.append(f"{spec.name}: no prompt file")
        for key in ("risk_class", "task_class"):
            if not f.get(key):
                missing.append(f"{spec.name}: no {key}")
        if not (f.get("flag") or {}).get("rollout") and "flag" not in f:
            missing.append(f"{spec.name}: no flag declaration")
        if not spec.eval.get("thresholds"):
            missing.append(f"{spec.name}: no thresholds")
        cases = spec.cases()
        min_cases = int(spec.eval.get("min_cases", 30))
        if len(cases) < min_cases:
            missing.append(f"{spec.name}: {len(cases)} eval cases < {min_cases}")
        if not any("injection" in c.get("tags", []) for c in cases):
            missing.append(f"{spec.name}: no prompt-injection cases (guardrail tests)")
        cassette = Cassette.load(spec.cassette_path)
        case_ids = {c["id"] for c in cases}
        recorded = {e["case"] for e in cassette.entries.values()}
        if cases and not case_ids <= recorded and not spec.eval.get("deterministic"):
            missing.append(f"{spec.name}: {len(case_ids - recorded)} cases without replay cassettes")
    return missing


def summary(report: EvalReport) -> dict[str, Any]:
    return {
        "feature": report.feature,
        "prompt_version": report.prompt_version,
        "mode": report.mode,
        "cases": report.cases,
        "metrics": report.metrics,
        "thresholds": report.thresholds,
        "passed": report.passed,
        "failures": report.failures,
        "latency_p95_ms": report.latency_p95_ms,
        "latency_median_ms": int(statistics.median([r["latency_ms"] for r in report.case_results] or [0])),
        "cost": report.cost,
    }
