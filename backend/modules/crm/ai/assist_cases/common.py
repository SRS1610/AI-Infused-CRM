"""Shared helpers of the CRM assist evaluation corpus."""

from __future__ import annotations

import json
from typing import Any

JUDGE = "eval.judge@1"
MAIN = "$xml:core.main_company"
EU = "$xml:core.demo_company_eu"


def resp(
    content: list[dict[str, Any]], stop: str = "end", parsed: Any = None, *, tokens: tuple[int, int] = (900, 180)
) -> dict[str, Any]:
    """A gateway response as the replay client returns it."""
    inp, out = tokens
    return {
        "id": "replay",
        "provider": "replay",
        "model": "replay-model",
        "content": content,
        "stop": stop,
        "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_tokens": 0, "cache_write_tokens": 0},
        "cost": f"{(inp * 0.8 + out * 4) / 1_000_000:.6f}",
        "latency_ms": 600 + out * 4,
        "parsed": parsed,
        "safety_flags": [],
    }


def text(body: str, tokens: tuple[int, int] | None = None) -> dict[str, Any]:
    return resp([{"type": "text", "text": body}], "end", None, tokens=tokens or (1200, max(60, len(body) // 4)))


def structured(data: Any, tokens: tuple[int, int] | None = None) -> dict[str, Any]:
    body = json.dumps(data, ensure_ascii=False)
    return resp([{"type": "text", "text": body}], "end", data, tokens=tokens or (1400, max(40, len(body) // 4)))


def tool_use(case: str, calls: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    blocks = [
        {"type": "tool_use", "id": f"toolu_{case}_{i}", "name": name, "input": args}
        for i, (name, args) in enumerate(calls)
    ]
    return resp(blocks, "tool_use", None, tokens=(1300, 40))


def entry(pv: str, case: str, index: int, response: dict[str, Any]) -> dict[str, Any]:
    return {"prompt_version": pv, "case": case, "index": index, "response": response}


def judged(case: str, index: int, score: int, reason: str) -> dict[str, Any]:
    parsed = {"score": score, "reason": reason}
    return entry(JUDGE, case, index, structured(parsed, tokens=(1100, 60)))


def html(body: str) -> str:
    """Plain text → the HTML the thread / description fields store."""
    import html as _html

    return "".join(f"<p>{_html.escape(p).replace(chr(10), '<br/>')}</p>" for p in body.strip().split("\n\n"))
