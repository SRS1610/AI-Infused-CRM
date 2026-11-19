"""OpenAI-family adapters on the official ``openai`` SDK (Chat Completions):

* ``openai`` — OpenAI's API;
* ``azure_openai`` — Azure OpenAI; ``options.deployments`` maps model names to deployment names and
  ``options.api_version`` pins the API version; residency is the Azure region of the endpoint;
* ``openai_compatible`` — self-hosted servers speaking the same protocol (vLLM, Ollama, TGI).
  Tool calling and JSON-schema output are feature-flagged per model (``options.supports_tools``,
  ``options.json_mode`` = ``schema`` | ``object`` | ``none``) because open-weight servers differ.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import openai

from gateway.adapters.base import Adapter, Capabilities, Chunk, ProviderRequest, ProviderResponse, fit_dimensions
from gateway.envelope import Block, EmbedResponse, Message, Stop, Usage
from gateway.errors import ProviderError

FINISH = {"stop": Stop.end, "length": Stop.max_tokens, "tool_calls": Stop.tool_use, "content_filter": Stop.refusal}


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": system}] if system else []
    for m in messages:
        if isinstance(m.content, str):
            out.append({"role": m.role, "content": m.content})
            continue
        texts = [b.text or "" for b in m.content if b.type == "text"]
        calls = [b for b in m.content if b.type == "tool_use"]
        results = [b for b in m.content if b.type == "tool_result"]
        images = [b for b in m.content if b.type == "image"]
        if m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": "".join(texts) or None}
            if calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.input or {})},
                    }
                    for c in calls
                ]
            out.append(msg)
            continue
        for r in results:
            content = r.content or ""
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": r.tool_use_id,
                    "content": f"ERROR: {content}" if r.is_error else content,
                }
            )
        if images:
            parts: list[dict[str, Any]] = [_image_part(b) for b in images]
            parts.append({"type": "text", "text": "".join(texts)})
            out.append({"role": "user", "content": parts})
        elif texts:
            out.append({"role": "user", "content": "".join(texts)})
    return out


def _image_part(b: Block) -> dict[str, Any]:
    url = f"data:{b.media_type};base64,{b.data or ''}"
    if b.media_type == "application/pdf":
        return {"type": "file", "file": {"filename": "document.pdf", "file_data": url}}
    return {"type": "image_url", "image_url": {"url": url}}


def _closed(schema: dict[str, Any]) -> bool:
    """Strict mode needs every object in the schema closed (``additionalProperties: false`` + ``required``)."""
    if schema.get("type") == "object" or "properties" in schema:
        if schema.get("additionalProperties") is not False or "required" not in schema:
            return False
        return all(_closed(v) for v in schema.get("properties", {}).values() if isinstance(v, dict))
    if schema.get("type") == "array" and isinstance(schema.get("items"), dict):
        return _closed(schema["items"])
    return True


class OpenAIAdapter(Adapter):
    kind = "openai"

    def __init__(self, config: Any, http_client: Any = None) -> None:
        super().__init__(config, http_client)
        self.client = self._client()

    def _client(self) -> Any:
        return openai.AsyncOpenAI(
            api_key=self.config.api_key or "missing-key",
            base_url=self.config.base_url or None,
            http_client=self.http_client,
            max_retries=int(self.config.options.get("max_retries", 1)),
            timeout=float(self.config.options.get("timeout", 60)),
        )

    def capabilities(self) -> Capabilities:
        return Capabilities(embeddings=True, prompt_cache=True)

    def _model(self, model: str) -> str:
        return model

    @property
    def json_mode(self) -> str:
        return str(self.config.options.get("json_mode", "schema"))

    @property
    def supports_tools(self) -> bool:
        return bool(self.config.options.get("supports_tools", True))

    def _params(self, req: ProviderRequest) -> dict[str, Any]:
        system = req.system
        params: dict[str, Any] = {"model": self._model(req.model), "max_completion_tokens": req.max_output_tokens}
        if req.output_schema:
            if self.json_mode == "schema":
                params["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "output",
                        "schema": req.output_schema,
                        "strict": _closed(req.output_schema),
                    },
                }
            else:
                # weaker servers: ask for JSON in the instructions (and JSON mode when available);
                # the gateway validates the result against the schema either way
                system = f"{system}\n\nAnswer with JSON matching this schema:\n{json.dumps(req.output_schema)}".strip()
                if self.json_mode == "object":
                    params["response_format"] = {"type": "json_object"}
        params["messages"] = to_openai_messages(system, req.messages)
        if req.tools and self.supports_tools:
            params["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                        **({"strict": True} if _closed(t.input_schema) else {}),
                    },
                }
                for t in req.tools
            ]
        if req.effort and self.config.options.get("reasoning_effort"):
            params["reasoning_effort"] = req.effort
        return params

    @staticmethod
    def _error(exc: Exception) -> ProviderError:
        if isinstance(exc, openai.APIStatusError):
            return ProviderError(
                str(exc.message), retryable=exc.status_code >= 500 or exc.status_code == 429, status=exc.status_code
            )
        if isinstance(exc, openai.APIConnectionError):
            return ProviderError(str(exc), retryable=True)
        return ProviderError(str(exc), retryable=False)

    @staticmethod
    def _usage(u: Any) -> Usage:
        if u is None:
            return Usage()
        details = getattr(u, "prompt_tokens_details", None)
        cached = (getattr(details, "cached_tokens", 0) or 0) if details else 0
        return Usage(
            input_tokens=(u.prompt_tokens or 0) - cached,
            output_tokens=u.completion_tokens or 0,
            cache_read_tokens=cached,
        )

    async def complete(self, req: ProviderRequest) -> ProviderResponse:
        try:
            resp = await self.client.chat.completions.create(**self._params(req))
        except openai.OpenAIError as exc:
            raise self._error(exc) from exc
        choice = resp.choices[0]
        msg = choice.message
        content: list[Block] = []
        if msg.content:
            content.append(Block(type="text", text=msg.content))
        for call in msg.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": call.function.arguments}
            content.append(Block(type="tool_use", id=call.id, name=call.function.name, input=args))
        stop = FINISH.get(choice.finish_reason or "stop", Stop.end)
        refusal = None
        if getattr(msg, "refusal", None):
            stop, refusal = Stop.refusal, "provider_refusal"
        return ProviderResponse(
            content, stop, self._usage(resp.usage), resp.model, raw_id=resp.id, refusal_category=refusal
        )

    async def stream(self, req: ProviderRequest) -> AsyncIterator[Chunk]:
        params = {**self._params(req), "stream": True, "stream_options": {"include_usage": True}}
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}
        finish, usage, model, raw_id = "stop", Usage(), req.model, ""
        try:
            stream = await self.client.chat.completions.create(**params)
            async for event in stream:
                model, raw_id = event.model or model, event.id or raw_id
                if event.usage:
                    usage = self._usage(event.usage)
                for choice in event.choices:
                    delta = choice.delta
                    if delta.content:
                        text_parts.append(delta.content)
                        yield Chunk(text=delta.content)
                    for tc in delta.tool_calls or []:
                        slot = calls.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                        slot["id"] = tc.id or slot["id"]
                        if tc.function:
                            slot["name"] = tc.function.name or slot["name"]
                            slot["arguments"] += tc.function.arguments or ""
                    if choice.finish_reason:
                        finish = choice.finish_reason
        except openai.OpenAIError as exc:
            raise self._error(exc) from exc
        content: list[Block] = [Block(type="text", text="".join(text_parts))] if text_parts else []
        for slot in calls.values():
            try:
                args = json.loads(slot["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {"_unparseable": slot["arguments"]}
            block = Block(type="tool_use", id=slot["id"], name=slot["name"], input=args)
            content.append(block)
            yield Chunk(block=block)
        yield Chunk(final=ProviderResponse(content, FINISH.get(finish, Stop.end), usage, model, raw_id=raw_id))

    async def embed(
        self, texts: list[str], model: str, *, input_type: str = "document", dimensions: int = 1024
    ) -> EmbedResponse:
        kwargs: dict[str, Any] = {"model": self._model(model), "input": texts}
        if self.config.options.get("supports_dimensions", self.kind != "openai_compatible"):
            kwargs["dimensions"] = dimensions
        try:
            resp = await self.client.embeddings.create(**kwargs)
        except openai.OpenAIError as exc:
            raise self._error(exc) from exc
        vectors = [fit_dimensions(list(d.embedding), dimensions) for d in sorted(resp.data, key=lambda d: d.index)]
        return EmbedResponse(
            provider=self.config.name,
            model=model,
            dimensions=dimensions,
            vectors=vectors,
            usage=Usage(input_tokens=resp.usage.prompt_tokens if resp.usage else 0),
        )

    async def aclose(self) -> None:
        await self.client.close()


class AzureOpenAIAdapter(OpenAIAdapter):
    kind = "azure_openai"

    def _client(self) -> Any:
        return openai.AsyncAzureOpenAI(
            api_key=self.config.api_key or "missing-key",
            azure_endpoint=self.config.base_url or "https://example.openai.azure.com",
            api_version=str(self.config.options.get("api_version", "2024-10-21")),
            http_client=self.http_client,
            max_retries=int(self.config.options.get("max_retries", 1)),
            timeout=float(self.config.options.get("timeout", 60)),
        )

    def _model(self, model: str) -> str:
        return str(self.config.options.get("deployments", {}).get(model, model))


class OpenAICompatibleAdapter(OpenAIAdapter):
    """vLLM / Ollama / TGI. Nothing leaves the deployment, so these providers are usually approved
    for PII by the administrator."""

    kind = "openai_compatible"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            tools=self.supports_tools, structured_output=self.json_mode != "none", embeddings=True, prompt_cache=False
        )
