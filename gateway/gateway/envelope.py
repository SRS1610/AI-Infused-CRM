"""The gateway's provider-neutral envelope.

The application (``tessera.ai``) speaks only these shapes; each adapter translates them to its
provider's native request and normalises the answer back (usage, stop reasons, tool calls).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class TaskClass(StrEnum):
    classify = "classify"
    extract = "extract"
    embed = "embed"
    summarize = "summarize"
    draft = "draft"
    reason = "reason"
    translation = "translate"
    nl_query = "nl_query"
    vision = "vision"
    transcribe = "transcribe"


class Stop(StrEnum):
    end = "end"
    max_tokens = "max_tokens"
    tool_use = "tool_use"
    refusal = "refusal"
    error = "error"


class Block(BaseModel):
    """One content block. ``text`` for text, ``tool_use`` (id/name/input) for a model's tool call,
    ``tool_result`` (tool_use_id/content/is_error) for the runtime's answer."""

    type: Literal["text", "tool_use", "tool_result"]
    text: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None
    tool_use_id: str | None = None
    content: str | None = None
    is_error: bool = False


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[Block]

    def blocks(self) -> list[Block]:
        if isinstance(self.content, str):
            return [Block(type="text", text=self.content)]
        return self.content


class ToolSpec(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any]


class Meta(BaseModel):
    feature: str = ""
    prompt_version: str = ""
    run_id: str = ""
    cache_ttl: int = 0  # seconds; 0 → never cached (drafts)
    cache_key_extra: str = ""  # e.g. write_date of the source records, so edits invalidate the cache
    redaction_names: list[str] = Field(default_factory=list)  # known person/company names in the context


class CompleteRequest(BaseModel):
    task_class: TaskClass
    system: str = ""
    messages: list[Message]
    tools: list[ToolSpec] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    max_output_tokens: int = 4096
    cache_hints: bool = True  # mark the stable prefix (tools + system) cacheable where the provider supports it
    redaction_policy: Literal["none", "redact", "minimal"] = "redact"
    metadata: Meta = Field(default_factory=Meta)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
        )


class CompleteResponse(BaseModel):
    id: str
    provider: str
    model: str
    content: list[Block]
    stop: Stop
    usage: Usage
    cost: Decimal = Decimal(0)
    latency_ms: int = 0
    parsed: Any = None  # JSON value when an output schema was requested
    fallback_used: bool = False
    cached: bool = False
    redaction_applied: bool = False
    repaired: bool = False
    inference_geo: str | None = None
    refusal_category: str | None = None
    safety_flags: list[str] = Field(default_factory=list)
    budget_warning: bool = False  # a soft budget threshold is crossed (administrators are alerted)

    @property
    def text(self) -> str:
        return "".join(b.text or "" for b in self.content if b.type == "text")


class EmbedRequest(BaseModel):
    texts: list[str]
    input_type: Literal["document", "query"] = "document"
    metadata: Meta = Field(default_factory=Meta)


class EmbedResponse(BaseModel):
    provider: str
    model: str
    dimensions: int
    vectors: list[list[float]]
    usage: Usage
    cost: Decimal = Decimal(0)


class ModerateRequest(BaseModel):
    text: str
    context: str = ""  # text the output was based on (URLs present there are not flagged)


class ModerateResponse(BaseModel):
    allowed: bool
    flags: list[str]
    reasons: list[str]


class StreamEvent(BaseModel):
    """Server-Sent Event payload: ``delta`` (text), ``tool_use`` (a complete call), ``usage``, ``done``
    (with the final response) or ``error``."""

    event: Literal["delta", "tool_use", "usage", "done", "error"]
    text: str | None = None
    block: Block | None = None
    usage: Usage | None = None
    response: CompleteResponse | None = None
    error: dict[str, Any] | None = None


EMBEDDING_DIMENSIONS = 1024  # platform-wide vector size (D-047)
