"""Structured-output validation against the feature's JSON Schema (one bounded repair attempt is
made by the service when this fails)."""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator

FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class SchemaMismatch(Exception):
    pass


def parse_json(text: str) -> Any:
    m = FENCE.match(text or "")
    body = m.group(1) if m else (text or "")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SchemaMismatch(f"output is not valid JSON: {exc.msg} at position {exc.pos}") from exc


def validate(value: Any, schema: dict[str, Any]) -> None:
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        where = "/".join(str(p) for p in first.path) or "(root)"
        raise SchemaMismatch(f"{where}: {first.message}" + (f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""))


def parse_and_validate(text: str, schema: dict[str, Any]) -> Any:
    value = parse_json(text)
    validate(value, schema)
    return value


def repair_message(error: str) -> str:
    return (
        "Your previous answer did not match the required JSON Schema: "
        f"{error}. Answer again with only the corrected JSON value, nothing else."
    )
