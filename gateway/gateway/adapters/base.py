"""Adapter interface. One adapter instance per provider configuration; adapters are stateless apart
from their HTTP client, so the gateway can share them across requests."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from gateway.config import ProviderConfig
from gateway.envelope import Block, EmbedResponse, Message, Stop, ToolSpec, Usage
from gateway.errors import NotSupported


@dataclass
class Capabilities:
    chat: bool = True
    stream: bool = True
    tools: bool = True
    structured_output: bool = True
    embeddings: bool = False
    prompt_cache: bool = False


@dataclass
class ProviderRequest:
    """What the gateway asks an adapter for, after routing and redaction."""

    model: str
    system: str
    messages: list[Message]
    tools: list[ToolSpec] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    max_output_tokens: int = 4096
    effort: str | None = None  # low | medium | high (reasoning depth where the provider has it)
    cache: bool = True
    params: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    content: list[Block]
    stop: Stop
    usage: Usage
    model: str
    raw_id: str = ""
    inference_geo: str | None = None
    refusal_category: str | None = None

    @property
    def text(self) -> str:
        return "".join(b.text or "" for b in self.content if b.type == "text")


@dataclass
class Chunk:
    """A streaming piece: text delta, a completed tool call, or the final response."""

    text: str | None = None
    block: Block | None = None
    final: ProviderResponse | None = None


class Adapter(ABC):
    kind: str = ""

    def __init__(self, config: ProviderConfig, http_client: Any = None) -> None:
        self.config = config
        self.http_client = http_client

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    async def complete(self, req: ProviderRequest) -> ProviderResponse: ...

    async def stream(self, req: ProviderRequest) -> AsyncIterator[Chunk]:
        """Default: no native streaming, deliver the whole answer as one delta."""
        resp = await self.complete(req)
        if resp.text:
            yield Chunk(text=resp.text)
        for b in resp.content:
            if b.type == "tool_use":
                yield Chunk(block=b)
        yield Chunk(final=resp)

    async def embed(
        self, texts: list[str], model: str, *, input_type: str = "document", dimensions: int = 1024
    ) -> EmbedResponse:
        raise NotSupported(f"{self.config.kind} does not provide embeddings")

    async def aclose(self) -> None:  # pragma: no cover - adapters without resources
        return None


def fit_dimensions(vector: list[float], dimensions: int) -> list[float]:
    """Zero-pad (cosine-preserving) or truncate-and-renormalise a vector to the platform size."""
    if len(vector) == dimensions:
        return vector
    if len(vector) < dimensions:
        return vector + [0.0] * (dimensions - len(vector))
    cut = vector[:dimensions]
    norm = sum(x * x for x in cut) ** 0.5 or 1.0
    return [x / norm for x in cut]


def tool_result_text(block: Block) -> str:
    return block.content or ""
