"""Helpers for AI features whose numbers come from the ledger (Phase 2 finance features).

* :func:`ensure_enabled` / :func:`feature_enabled` — kill switches for code paths that compute without a
  provider call (deterministic rankers, statistical checks).
* :func:`audit_run` — the ``ai_core.run`` of such a computation (audit trail, decisions, feedback).
* :func:`fact_numbers` / :func:`numbers_in` / :func:`ungrounded` / :func:`ground_text` — **numeric
  consistency**: every number a model writes must be one of the numbers of the facts it was given
  (amounts, counts, days, percentages, dates). :func:`ground_text` removes the sentences that carry a
  number the facts do not contain, so a text shown to a person never states a number the ledger did not
  produce; the evaluation scorers use :func:`ungrounded` to count violations.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

PROVIDER = "Tessera (in-process)"


# ----------------------------------------------------------------------------------------- kill switches


def ai_installed(env: Environment) -> bool:
    return "ai_core.feature" in env.registry.models


def feature_enabled(env: Environment, name: str, company_id: int | None = None) -> bool:
    """Whether ``name`` is on for the company (and installed); never raises."""
    if not ai_installed(env):
        return False
    feature = env["ai_core.feature"].elevated().search([("name", "=", name)], limit=1)
    if company_id is None and env.company:
        company_id = env.company.ids[0]
    return bool(feature) and bool(feature.enabled_for(company_id))


def ensure_enabled(env: Environment, name: str) -> Model:
    """Kill switches and internal-user check (raises ``FeatureDisabled`` / ``AccessError``)."""
    from tessera.ai.errors import FeatureDisabled
    from tessera.ai.runtime import check_enabled, get_feature

    if not ai_installed(env):
        raise FeatureDisabled(_("AI is not installed"))
    feature = get_feature(env, name)
    check_enabled(env, feature)
    return feature


def audit_run(
    env: Environment,
    name: str,
    *,
    record: Model | None = None,
    inputs: dict[str, Any] | None = None,
    output: Any = None,
    text: str = "",
    model: str = "",
    started: float | None = None,
) -> Model:
    """An ``ai_core.run`` for a computation that made no provider call."""
    feature = env["ai_core.feature"].elevated().search([("name", "=", name)], limit=1)
    latency = int((time.monotonic() - started) * 1000) if started is not None else 0
    return (
        env["ai_core.run"]
        .elevated("ai run")
        .create(
            {
                "feature_id": feature.ids[0] if feature else None,
                "feature": name,
                "user_id": env.uid,
                "company_id": env.company.ids[0] if env.company else None,
                "provider": PROVIDER,
                "model": model,
                "status": "ok",
                "res_model": record._name if record is not None else None,
                "res_id": record.ids[0] if record is not None and record.ids else None,
                "input_ref": {"inputs": json.loads(json.dumps(inputs or {}, default=str))},
                "output_text": text,
                "output_data": json.loads(json.dumps(output, default=str)) if output is not None else None,
                "latency_ms": latency,
                "cost": 0.0,
                "steps": 0,
            }
        )
    )


# ----------------------------------------------------------------------------------------- numbers

# a number as people write it: 1,234.56 / 1.234,56 / 1 234,56 / 1234 / 12.5 / -3 (not glued to letters)
_NUM = r"\d{1,3}(?:[,.  ' ]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d+)?"
NUMBER = re.compile(
    rf"(?<![\w.,/:])(?P<num>{_NUM})(?P<scale>\s?(?:k|m|mn|million|thousand)\b)?(?![\w/:]|[.,]\d)",
    re.IGNORECASE,
)
ISO_DATE = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
SLASH_DATE = re.compile(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})\b")
TIME = re.compile(r"\b\d{1,2}:\d{2}\b")
TOLERANCE = 0.005


def parse_number(raw: str) -> float | None:
    """``18,900.00`` / ``18.900,00`` / ``18 900`` / ``113.4`` → float."""
    s = re.sub(r"[  ' ]", "", raw)
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        thousands = len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and len(parts[0]) <= 3)
        s = s.replace(sep, "") if thousands else s.replace(sep, ".")
    try:
        return float(s)
    except ValueError:
        return None


def _scaled(value: float, suffix: str | None) -> float:
    sx = (suffix or "").strip().lower()
    if sx in ("k", "thousand"):
        return value * 1_000
    if sx in ("m", "mn", "million"):
        return value * 1_000_000
    return value


def numbers_in(text: str) -> list[tuple[str, float]]:
    """Every number written in ``text`` (dates contribute their parts): ``[(as written, value)]``."""
    text = text or ""
    out: list[tuple[str, float]] = []
    spans: list[tuple[int, int]] = []
    for m in ISO_DATE.finditer(text):
        spans.append(m.span())
        out += [(m.group(0), float(g)) for g in m.groups()]
    for m in SLASH_DATE.finditer(text):
        spans.append(m.span())
        out += [(m.group(0), float(g)) for g in m.groups()]
    spans += [m.span() for m in TIME.finditer(text)]
    for m in NUMBER.finditer(text):
        start, end = m.span("num")
        if any(s <= start < e for s, e in spans):
            continue
        value = parse_number(m.group("num"))
        if value is not None:
            out.append((m.group(0).strip(), _scaled(value, m.group("scale"))))
    return out


def fact_numbers(facts: Any) -> set[float]:
    """Every number of a facts structure (dicts, lists, numbers, ISO dates and numbers inside strings),
    with absolute values."""
    found: set[float] = set()

    def walk(value: Any) -> None:
        if isinstance(value, bool) or value is None:
            return
        if isinstance(value, int | float):
            found.add(round(abs(float(value)), 6))
        elif isinstance(value, dt.date):
            found.update({float(value.year), float(value.month), float(value.day)})
        elif isinstance(value, str):
            for _raw, v in numbers_in(value):
                found.add(round(abs(v), 6))
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v)
                if isinstance(k, str):
                    walk(k)
        elif isinstance(value, Iterable):
            for v in value:
                walk(v)

    walk(facts)
    return found


def grounded(value: float, known: set[float]) -> bool:
    """``value`` is a fact, or a fact rounded to a whole number or to one decimal."""
    v = abs(value)
    for k in known:
        if abs(v - k) <= TOLERANCE:
            return True
        if float(v).is_integer() and abs(v - round(k)) <= TOLERANCE and abs(k - v) < 1:
            return True
        if abs(v - round(k, 1)) <= TOLERANCE and abs(k - v) < 0.1:
            return True
    return False


def ungrounded(text: str, facts: Any, *, allow: Iterable[float] = ()) -> list[str]:
    """Numbers written in ``text`` that are none of the facts' numbers (as written)."""
    known = fact_numbers(facts) | {round(abs(float(a)), 6) for a in allow}
    return [raw for raw, value in numbers_in(text) if not grounded(value, known)]


def ground_text(text: str, facts: Any, *, allow: Iterable[float] = ()) -> tuple[str, list[str]]:
    """(text without the sentences that state a number absent from the facts, the removed numbers)."""
    allowed = list(allow)
    removed: list[str] = []
    kept: list[str] = []
    for part in re.split(r"(\n+)", text or ""):
        if not part.strip() or part.startswith("\n"):
            kept.append(part)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", part)
        good = []
        for sentence in sentences:
            bad = ungrounded(sentence, facts, allow=allowed)
            if bad:
                removed += bad
            else:
                good.append(sentence)
        kept.append(" ".join(good))
    out = re.sub(r"\n{3,}", "\n\n", "".join(kept)).strip()
    return out, removed


# ----------------------------------------------------------------------------------------- cassettes

JUDGE_PROMPT = "eval.judge@1"


def replay_response(
    parsed: Any = None, text: str | None = None, *, tokens: tuple[int, int] = (900, 160)
) -> dict[str, Any]:
    """A gateway response as the replay client returns it (hand-authored answers of the build scripts)."""
    body = text if text is not None else json.dumps(parsed, ensure_ascii=False)
    inp, out = tokens
    return {
        "id": "replay",
        "provider": "replay",
        "model": "replay-model",
        "content": [{"type": "text", "text": body}],
        "stop": "end",
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_tokens": 0, "cache_write_tokens": 0},
        "cost": f"{(inp * 0.8 + out * 4) / 1_000_000:.6f}",
        "latency_ms": 500 + out * 3,
        "parsed": parsed,
        "safety_flags": [],
    }


def cassette_entry(prompt_version: str, case: str, index: int, response: dict[str, Any]) -> dict[str, Any]:
    return {"prompt_version": prompt_version, "case": case, "index": index, "response": response}


def judge_entry(case: str, index: int, score: int, reason: str) -> dict[str, Any]:
    """The replayed LLM-as-judge grade of a case (D-058)."""
    response = replay_response({"score": score, "reason": reason}, tokens=(1100, 60))
    return cassette_entry(JUDGE_PROMPT, case, index, response)


def write_jsonl(path: Any, rows: list[dict[str, Any]]) -> None:
    from pathlib import Path

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows))
