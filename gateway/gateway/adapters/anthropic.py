"""Anthropic Messages API adapter (official ``anthropic`` SDK).

Facts relied on (AI architecture note §3.3, re-verified against SDK 2.7 when pinned):

* tiers: ``claude-haiku-5-5`` (small), ``claude-sonnet-5-5`` (mid), ``claude-opus-5-5`` (large);
* thinking is adaptive by default on these models and cannot be switched off on Opus 5.5, so the
  adapter never sends ``thinking``; depth is set with ``output_config.effort`` per task class;
* forced ``tool_choice`` is rejected on Opus 5.5 / Sonnet 5.5, so tools are offered with ``auto``
  and declared ``strict`` when their schema is closed; JSON answers use ``output_config.format``;
* refusals arrive as HTTP 200 with ``stop_reason == "refusal"``; on Opus 5.5 and Sonnet 5.5 the
  server-side fallback (``fallbacks: "default"``, beta ``server-side-fallback-2026-07-01``) is
  enabled so a policy decline is retried on the provider's fallback model inside the same call;
* ``inference_geo`` is a top-level request parameter and ``usage.inference_geo`` reports where the
  request ran (stored in the ledger);
* prompt caching: ``cache_control`` breakpoints on the last tool and the system prompt.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import anthropic

from gateway.adapters.base import Adapter, Capabilities, Chunk, ProviderRequest, ProviderResponse
from gateway.envelope import Block, Message, Stop, Usage
from gateway.errors import ProviderError

SERVER_FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"
STOP_MAP = {
    "end_turn": Stop.end,
    "stop_sequence": Stop.end,
    "pause_turn": Stop.end,
    "max_tokens": Stop.max_tokens,
    "tool_use": Stop.tool_use,
    "refusal": Stop.refusal,
}


def _closed(schema: dict[str, Any]) -> bool:
    """Strict mode needs every object in the schema closed (``additionalProperties: false`` + ``required``)."""
    if schema.get("type") == "object" or "properties" in schema:
        if schema.get("additionalProperties") is not False or "required" not in schema:
            return False
        return all(_closed(v) for v in schema.get("properties", {}).values() if isinstance(v, dict))
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return _closed(schema["items"])
    return True


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
            continue
        blocks: list[dict[str, Any]] = []
        for b in m.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text or ""})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input or {}})
            elif b.type == "image":
                kind = "document" if b.media_type == "application/pdf" else "image"
                source = {"type": "base64", "media_type": b.media_type, "data": b.data or ""}
                blocks.append({"type": kind, "source": source})
            else:
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": b.tool_use_id,
                        "content": b.content or "",
                        "is_error": b.is_error,
                    }
                )
        out.append({"role": m.role, "content": blocks})
    return out


def from_anthropic_message(msg: Any) -> ProviderResponse:
    content: list[Block] = []
    for b in msg.content:
        if b.type == "text":
            content.append(Block(type="text", text=b.text))
        elif b.type == "tool_use":
            content.append(Block(type="tool_use", id=b.id, name=b.name, input=dict(b.input or {})))
        # thinking, fallback and other block types carry no answer content for the runtime
    u = msg.usage
    usage = Usage(
        input_tokens=u.input_tokens or 0,
        output_tokens=u.output_tokens or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", None) or 0,
    )
    details = getattr(msg, "stop_details", None)
    return ProviderResponse(
        content=content,
        stop=STOP_MAP.get(msg.stop_reason or "end_turn", Stop.end),
        usage=usage,
        model=msg.model,
        raw_id=msg.id,
        inference_geo=getattr(u, "inference_geo", None),
        refusal_category=getattr(details, "category", None) if details else None,
    )


class AnthropicAdapter(Adapter):
    kind = "anthropic"

    def __init__(self, config: Any, http_client: Any = None) -> None:
        super().__init__(config, http_client)
        self.client = anthropic.AsyncAnthropic(
            api_key=config.api_key or "missing-key",
            base_url=config.base_url or None,
            http_client=http_client,
            max_retries=int(config.options.get("max_retries", 1)),
            timeout=float(config.options.get("timeout", 60)),
        )

    def capabilities(self) -> Capabilities:
        return Capabilities(embeddings=False, prompt_cache=True)

    def _params(self, req: ProviderRequest) -> dict[str, Any]:
        system: list[dict[str, Any]] = []
        if req.system:
            block: dict[str, Any] = {"type": "text", "text": req.system}
            if req.cache:
                block["cache_control"] = {"type": "ephemeral"}
            system.append(block)
        tools = []
        for i, t in enumerate(req.tools):
            spec: dict[str, Any] = {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            if _closed(t.input_schema):
                spec["strict"] = True
            if req.cache and i == len(req.tools) - 1:
                spec["cache_control"] = {"type": "ephemeral"}
            tools.append(spec)
        output_config: dict[str, Any] = {}
        if req.effort:
            output_config["effort"] = req.effort
        if req.output_schema:
            output_config["format"] = {"type": "json_schema", "schema": req.output_schema}
        params: dict[str, Any] = {
            "model": req.model,
            "max_tokens": req.max_output_tokens,
            "messages": to_anthropic_messages(req.messages),
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = tools
        if output_config:
            params["output_config"] = output_config
        geo = self.config.options.get("inference_geo")
        if geo:
            params["inference_geo"] = geo
        if req.model in SERVER_FALLBACK_MODELS and self.config.options.get("server_fallback", True):
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    @staticmethod
    def _error(exc: Exception) -> ProviderError:
        if isinstance(exc, anthropic.APIStatusError):
            return ProviderError(
                str(exc.message), retryable=exc.status_code >= 500 or exc.status_code == 429, status=exc.status_code
            )
        if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
            return ProviderError(str(exc), retryable=True)
        return ProviderError(str(exc), retryable=False)

    async def complete(self, req: ProviderRequest) -> ProviderResponse:
        try:
            msg = await self.client.beta.messages.create(**self._params(req))
        except anthropic.AnthropicError as exc:
            raise self._error(exc) from exc
        return from_anthropic_message(msg)

    async def stream(self, req: ProviderRequest) -> AsyncIterator[Chunk]:
        try:
            async with self.client.beta.messages.stream(**self._params(req)) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield Chunk(text=event.text)
                final = await stream.get_final_message()
        except anthropic.AnthropicError as exc:
            raise self._error(exc) from exc
        resp = from_anthropic_message(final)
        for b in resp.content:
            if b.type == "tool_use":
                yield Chunk(block=b)
        yield Chunk(final=resp)

    async def count_tokens(self, req: ProviderRequest) -> int:
        params = self._params(req)
        for key in ("max_tokens", "betas", "fallbacks", "inference_geo"):
            params.pop(key, None)
        result = await self.client.messages.count_tokens(**params)
        return int(result.input_tokens)

    async def aclose(self) -> None:
        await self.client.close()
