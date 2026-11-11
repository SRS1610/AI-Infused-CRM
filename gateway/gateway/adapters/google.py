"""Google Gemini adapter over the Generative Language REST API (``generateContent``,
``streamGenerateContent?alt=sse``, ``batchEmbedContents``), using ``httpx2``.

The REST surface is used instead of the ``google-genai`` SDK to keep the gateway's dependency set
small and its contract tests transport-level (D-046). Vertex AI deployments set ``base_url`` to the
regional Vertex endpoint; residency then follows that region.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx2

from gateway.adapters.base import Adapter, Capabilities, Chunk, ProviderRequest, ProviderResponse, fit_dimensions
from gateway.envelope import Block, EmbedResponse, Message, Stop, Usage
from gateway.errors import ProviderError

DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta"
FINISH = {"STOP": Stop.end, "MAX_TOKENS": Stop.max_tokens, "SAFETY": Stop.refusal, "PROHIBITED_CONTENT": Stop.refusal}
UNSUPPORTED_SCHEMA_KEYS = {"additionalProperties", "$schema", "$defs", "title", "default"}


def clean_schema(schema: Any) -> Any:
    """Gemini accepts an OpenAPI subset: drop keywords it rejects (but never property *names*, so a
    field called ``title`` or ``default`` survives)."""
    if isinstance(schema, dict):
        return {
            k: {name: clean_schema(sub) for name, sub in v.items()}
            if k == "properties" and isinstance(v, dict)
            else clean_schema(v)
            for k, v in schema.items()
            if k not in UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(schema, list):
        return [clean_schema(v) for v in schema]
    return schema


def to_gemini_contents(messages: list[Message]) -> list[dict[str, Any]]:
    names: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for m in messages:
        parts: list[dict[str, Any]] = []
        for b in m.blocks():
            if b.type == "text":
                parts.append({"text": b.text or ""})
            elif b.type == "tool_use":
                names[b.id or ""] = b.name or ""
                parts.append({"functionCall": {"name": b.name, "args": b.input or {}}})
            elif b.type == "image":
                parts.append({"inlineData": {"mimeType": b.media_type, "data": b.data or ""}})
            else:
                parts.append(
                    {
                        "functionResponse": {
                            "name": names.get(b.tool_use_id or "", "tool"),
                            "response": {"error" if b.is_error else "result": b.content or ""},
                        }
                    }
                )
        out.append({"role": "model" if m.role == "assistant" else "user", "parts": parts})
    return out


class GoogleAdapter(Adapter):
    kind = "google"

    def __init__(self, config: Any, http_client: Any = None) -> None:
        super().__init__(config, http_client)
        self.client: httpx2.AsyncClient = http_client or httpx2.AsyncClient(
            timeout=float(config.options.get("timeout", 60))
        )
        self.base = (config.base_url or DEFAULT_BASE).rstrip("/")

    def capabilities(self) -> Capabilities:
        return Capabilities(embeddings=True, prompt_cache=False)

    def _headers(self) -> dict[str, str]:
        return {"x-goog-api-key": self.config.api_key or "", "content-type": "application/json"}

    def _body(self, req: ProviderRequest) -> dict[str, Any]:
        body: dict[str, Any] = {"contents": to_gemini_contents(req.messages)}
        if req.system:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}
        gen: dict[str, Any] = {"maxOutputTokens": req.max_output_tokens}
        if req.output_schema:
            gen["responseMimeType"] = "application/json"
            gen["responseSchema"] = clean_schema(req.output_schema)
        body["generationConfig"] = gen
        if req.tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {"name": t.name, "description": t.description, "parameters": clean_schema(t.input_schema)}
                        for t in req.tools
                    ]
                }
            ]
        return body

    async def _post(self, url: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            resp = await self.client.post(url, json=body, headers=self._headers())
        except httpx2.TransportError as exc:
            raise ProviderError(str(exc), retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError(
                resp.text[:500], retryable=resp.status_code >= 500 or resp.status_code == 429, status=resp.status_code
            )
        data: dict[str, Any] = resp.json()
        return data

    @staticmethod
    def _parse(data: dict[str, Any], model: str) -> ProviderResponse:
        content: list[Block] = []
        candidates = data.get("candidates") or [{}]
        cand = candidates[0]
        for i, part in enumerate((cand.get("content") or {}).get("parts", [])):
            if "text" in part:
                content.append(Block(type="text", text=part["text"]))
            elif "functionCall" in part:
                fc = part["functionCall"]
                content.append(Block(type="tool_use", id=f"call_{i}", name=fc["name"], input=fc.get("args", {})))
        reason = cand.get("finishReason", "STOP")
        stop = FINISH.get(reason, Stop.end)
        if any(b.type == "tool_use" for b in content):
            stop = Stop.tool_use
        if data.get("promptFeedback", {}).get("blockReason"):
            stop = Stop.refusal
        um = data.get("usageMetadata", {})
        cached = um.get("cachedContentTokenCount", 0)
        usage = Usage(
            input_tokens=um.get("promptTokenCount", 0) - cached,
            # thinking tokens are billed as output but reported apart from the candidates
            output_tokens=um.get("candidatesTokenCount", 0) + um.get("thoughtsTokenCount", 0),
            cache_read_tokens=cached,
        )
        return ProviderResponse(
            content,
            stop,
            usage,
            data.get("modelVersion", model),
            raw_id=data.get("responseId", ""),
            refusal_category=reason.lower() if stop == Stop.refusal else None,
        )

    async def complete(self, req: ProviderRequest) -> ProviderResponse:
        data = await self._post(f"{self.base}/models/{req.model}:generateContent", self._body(req))
        return self._parse(data, req.model)

    async def stream(self, req: ProviderRequest) -> AsyncIterator[Chunk]:
        url = f"{self.base}/models/{req.model}:streamGenerateContent?alt=sse"
        merged: dict[str, Any] = {"candidates": [{"content": {"parts": []}}]}
        try:
            async with self.client.stream("POST", url, json=self._body(req), headers=self._headers()) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode()[:500]
                    raise ProviderError(
                        body, retryable=resp.status_code >= 500 or resp.status_code == 429, status=resp.status_code
                    )
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    piece = json.loads(line[5:].strip())
                    cand = (piece.get("candidates") or [{}])[0]
                    for part in (cand.get("content") or {}).get("parts", []):
                        merged["candidates"][0]["content"]["parts"].append(part)
                        if "text" in part:
                            yield Chunk(text=part["text"])
                    if cand.get("finishReason"):
                        merged["candidates"][0]["finishReason"] = cand["finishReason"]
                    for key in ("usageMetadata", "modelVersion", "responseId", "promptFeedback"):
                        if key in piece:
                            merged[key] = piece[key]
        except httpx2.TransportError as exc:
            raise ProviderError(str(exc), retryable=True) from exc
        # merge consecutive text parts into one block
        parts = merged["candidates"][0]["content"]["parts"]
        text = "".join(p["text"] for p in parts if "text" in p)
        merged["candidates"][0]["content"]["parts"] = ([{"text": text}] if text else []) + [
            p for p in parts if "text" not in p
        ]
        final = self._parse(merged, req.model)
        for b in final.content:
            if b.type == "tool_use":
                yield Chunk(block=b)
        yield Chunk(final=final)

    async def embed(
        self, texts: list[str], model: str, *, input_type: str = "document", dimensions: int = 1024
    ) -> EmbedResponse:
        task = "RETRIEVAL_QUERY" if input_type == "query" else "RETRIEVAL_DOCUMENT"
        body = {
            "requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": t}]},
                    "taskType": task,
                    "outputDimensionality": dimensions,
                }
                for t in texts
            ]
        }
        data = await self._post(f"{self.base}/models/{model}:batchEmbedContents", body)
        vectors = [fit_dimensions(e["values"], dimensions) for e in data.get("embeddings", [])]
        return EmbedResponse(
            provider=self.config.name,
            model=model,
            dimensions=dimensions,
            vectors=vectors,
            usage=Usage(input_tokens=sum(len(t) // 4 for t in texts)),
        )

    async def aclose(self) -> None:
        await self.client.aclose()
