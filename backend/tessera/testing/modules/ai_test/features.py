"""Context builders of the test features."""

from __future__ import annotations

from typing import Any

from tessera.ai import Source
from tessera.ai.runtime import handler, propose_from_output


def _ticket_source(record: Any) -> Source:
    rec = record.elevated()
    text = (
        f"Customer: {rec.customer_name or ''} <{rec.customer_email or ''}>\nState: {rec.state}\n{rec.description or ''}"
    )
    return Source(f"ai_test.ticket:{rec.ids[0]}", rec.name, text)


@handler("ai_test.summarize")
def summarize(env: Any, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    record.check_access("read")
    return {"sources": [_ticket_source(record)]}


@handler("ai_test.classify")
def classify(env: Any, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    return {"sources": [_ticket_source(record)]} if record is not None else {}


@handler("ai_test.draft")
def draft(env: Any, record: Any, inputs: dict[str, Any]) -> dict[str, Any]:
    record.check_access("read")
    return {
        "sources": [_ticket_source(record)],
        "on_output": propose_from_output(
            "message", record._name, record.ids[0], {}, f"Reply on {record.elevated().name}"
        ),
    }
