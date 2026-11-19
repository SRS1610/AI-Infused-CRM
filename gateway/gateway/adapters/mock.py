"""Deterministic in-process adapter for tests, local development and CI evaluation.

Behaviour is fully determined by the request and the provider's ``options``:

* ``script``: a list of rules ``{"match": {"feature": ..., "contains": ...}, "respond": {...}}``.
  The first matching rule answers. ``respond`` may give ``text``, ``json`` (serialised as the
  answer), ``tool_calls`` (``[{"name", "input"}]``, only while the conversation has no tool
  result yet), ``stop``, ``refusal`` (category), ``invalid_json_times`` (answer with text that
  breaks the schema the first *n* times) and ``usage``.
* ``fail_status`` + ``fail_times``: raise a provider error for the first *n* calls (``-1`` = always),
  ``delay_ms``: sleep before answering (timeouts), ``chunk_delay_ms``: pause between stream chunks.

Without a rule the adapter answers schema-valid JSON synthesised from the output schema, or a short
text echoing the task. Every request is captured in ``MockAdapter.captured`` so tests can assert
exactly what would have left the deployment (redaction).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from gateway.adapters.base import Adapter, Capabilities, Chunk, ProviderRequest, ProviderResponse
from gateway.envelope import Block, EmbedResponse, Stop, Usage
from gateway.errors import ProviderError

WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4)) if text else 0


def synthesize(schema: dict[str, Any], key: str = "") -> Any:
    """A deterministic instance that validates against a (simple) JSON Schema."""
    if "const" in schema:
        return schema["const"]
    if schema.get("enum"):
        return schema["enum"][0]
    if "anyOf" in schema:
        return synthesize(schema["anyOf"][0], key)
    if "oneOf" in schema:
        return synthesize(schema["oneOf"][0], key)
    typ = schema.get("type")
    if isinstance(typ, list):
        typ = next((t for t in typ if t != "null"), "null")
    if typ == "object":
        props = schema.get("properties", {})
        return {k: synthesize(v, k) for k, v in props.items() if k in schema.get("required", list(props))}
    if typ == "array":
        n = schema.get("minItems", 0)
        return [synthesize(schema.get("items", {}), key) for _ in range(n)]
    if typ == "string":
        if schema.get("format") == "date":
            return "2026-01-01"
        if schema.get("format") == "date-time":
            return "2026-01-01T09:00:00Z"
        return key or "mock"
    if typ == "integer":
        return int(schema.get("minimum", 0))
    if typ == "number":
        return float(schema.get("minimum", 0))
    if typ == "boolean":
        return False
    return None


def hash_embedding(text: str, dimensions: int) -> list[float]:
    """Bag-of-words feature hashing: texts sharing words are close, so retrieval tests are meaningful."""
    vec = [0.0] * dimensions
    for word in WORD.findall(text.lower()):
        h = int.from_bytes(hashlib.blake2b(word.encode(), digest_size=8).digest(), "big")
        vec[h % dimensions] += 1.0 if (h >> 63) == 0 else -1.0
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


class MockAdapter(Adapter):
    kind = "mock"
    captured: ClassVar[list[dict[str, Any]]] = []
    _calls: ClassVar[dict[int, int]] = {}
    _rule_hits: ClassVar[dict[tuple[int, int], int]] = {}

    @classmethod
    def reset(cls) -> None:
        cls.captured.clear()
        cls._calls.clear()
        cls._rule_hits.clear()

    def capabilities(self) -> Capabilities:
        return Capabilities(embeddings=True, prompt_cache=True)

    def _all_text(self, req: ProviderRequest) -> str:
        parts = [req.system]
        for m in req.messages:
            for b in m.blocks():
                parts.append(b.text or b.content or (json.dumps(b.input) if b.input else ""))
        return "\n".join(p for p in parts if p)

    async def _before_call(self, req: ProviderRequest) -> None:
        opts = self.config.options
        MockAdapter.captured.append(
            {
                "provider": self.config.name,
                "model": req.model,
                "system": req.system,
                "messages": [m.model_dump() for m in req.messages],
                "tools": [t.name for t in req.tools],
                "output_schema": req.output_schema,
                "effort": req.effort,
                "metadata": req.metadata,
            }
        )
        n = MockAdapter._calls.get(self.config.id, 0) + 1
        MockAdapter._calls[self.config.id] = n
        if opts.get("delay_ms"):
            await asyncio.sleep(opts["delay_ms"] / 1000)
        fail_times = opts.get("fail_times", 0)
        if opts.get("fail_status") and (fail_times == -1 or n <= fail_times):
            status = int(opts["fail_status"])
            raise ProviderError(
                f"mock provider failure {status}", retryable=status >= 500 or status == 429, status=status
            )

    def _rule(self, req: ProviderRequest) -> tuple[int, dict[str, Any]] | None:
        text = self._all_text(req)
        for i, rule in enumerate(self.config.options.get("script", [])):
            match = rule.get("match", {})
            if "feature" in match and match["feature"] != req.metadata.get("feature"):
                continue
            if "contains" in match and match["contains"] not in text:
                continue
            return i, rule.get("respond", {})
        return None

    def _answer(self, req: ProviderRequest) -> ProviderResponse:
        has_tool_result = any(b.type == "tool_result" for m in req.messages for b in m.blocks())
        found = self._rule(req)
        respond: dict[str, Any] = found[1] if found else {}
        content: list[Block] = []
        stop = Stop.end
        refusal = None
        if respond.get("refusal"):
            stop, refusal = Stop.refusal, respond["refusal"]
        elif respond.get("tool_calls") and not has_tool_result and req.tools:
            for i, call in enumerate(respond["tool_calls"]):
                content.append(
                    Block(type="tool_use", id=f"toolu_mock_{i}", name=call["name"], input=call.get("input", {}))
                )
            stop = Stop.tool_use
        else:
            invalid_times = respond.get("invalid_json_times", 0)
            if found and invalid_times:
                key = (self.config.id, found[0])
                hits = MockAdapter._rule_hits.get(key, 0) + 1
                MockAdapter._rule_hits[key] = hits
                if invalid_times == -1 or hits <= invalid_times:
                    content.append(Block(type="text", text='{"not": "what the schema asked for"'))
                    return self._finish(req, content, Stop.end, None)
            if "json" in respond:
                content.append(Block(type="text", text=json.dumps(respond["json"])))
            elif "text" in respond:
                content.append(Block(type="text", text=str(respond["text"])))
            elif req.output_schema:
                content.append(Block(type="text", text=json.dumps(synthesize(req.output_schema))))
            else:
                last = next(
                    (
                        b.text or b.content or ""
                        for m in reversed(req.messages)
                        for b in m.blocks()
                        if b.text or b.content
                    ),
                    "",
                )
                content.append(Block(type="text", text=f"Mock answer to: {last[:120]}"))
            if respond.get("stop") == "max_tokens":
                stop = Stop.max_tokens
        return self._finish(req, content, stop, refusal, respond.get("usage"))

    def _finish(
        self,
        req: ProviderRequest,
        content: list[Block],
        stop: Stop,
        refusal: str | None,
        usage: dict[str, int] | None = None,
    ) -> ProviderResponse:
        out_text = "".join(b.text or json.dumps(b.input or {}) for b in content)
        prompt_tokens = _tokens(self._all_text(req)) + sum(_tokens(json.dumps(t.input_schema)) for t in req.tools)
        cached = prompt_tokens // 2 if req.cache and req.metadata.get("warm_cache") else 0
        u = Usage(input_tokens=prompt_tokens - cached, output_tokens=_tokens(out_text), cache_read_tokens=cached)
        if usage:
            u = Usage(**usage)
        digest = hashlib.sha256((req.model + self._all_text(req)).encode()).hexdigest()[:16]
        return ProviderResponse(
            content=content,
            stop=stop,
            usage=u,
            model=req.model,
            raw_id=f"mock_{digest}",
            inference_geo=self.config.options.get("inference_geo"),
            refusal_category=refusal,
        )

    async def complete(self, req: ProviderRequest) -> ProviderResponse:
        await self._before_call(req)
        return self._answer(req)

    async def stream(self, req: ProviderRequest) -> AsyncIterator[Chunk]:
        await self._before_call(req)
        resp = self._answer(req)
        delay = self.config.options.get("chunk_delay_ms", 0) / 1000
        words = re.findall(r"\S+\s*", resp.text)
        for i in range(0, len(words), 3):
            if delay:
                await asyncio.sleep(delay)
            yield Chunk(text="".join(words[i : i + 3]))
        for b in resp.content:
            if b.type == "tool_use":
                yield Chunk(block=b)
        yield Chunk(final=resp)

    async def embed(
        self, texts: list[str], model: str, *, input_type: str = "document", dimensions: int = 1024
    ) -> EmbedResponse:
        await self._before_call(
            ProviderRequest(model=model, system="", messages=[], metadata={"embed": True, "count": len(texts)})
        )
        vectors = [hash_embedding(t, dimensions) for t in texts]
        return EmbedResponse(
            provider=self.config.name,
            model=model,
            dimensions=dimensions,
            vectors=vectors,
            usage=Usage(input_tokens=sum(_tokens(t) for t in texts)),
        )
