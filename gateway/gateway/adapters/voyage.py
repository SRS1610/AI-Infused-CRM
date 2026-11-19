"""Voyage AI embeddings (REST ``/v1/embeddings``) — the usual pairing for Anthropic deployments,
since Anthropic has no embeddings API."""

from __future__ import annotations

from typing import Any

import httpx2

from gateway.adapters.base import Adapter, Capabilities, ProviderRequest, ProviderResponse, fit_dimensions
from gateway.envelope import EmbedResponse, Usage
from gateway.errors import NotSupported, ProviderError

DEFAULT_BASE = "https://api.voyageai.com/v1"


class VoyageAdapter(Adapter):
    kind = "voyage"

    def __init__(self, config: Any, http_client: Any = None) -> None:
        super().__init__(config, http_client)
        self.client: httpx2.AsyncClient = http_client or httpx2.AsyncClient(
            timeout=float(config.options.get("timeout", 30))
        )
        self.base = (config.base_url or DEFAULT_BASE).rstrip("/")

    def capabilities(self) -> Capabilities:
        return Capabilities(chat=False, stream=False, tools=False, structured_output=False, embeddings=True)

    async def complete(self, req: ProviderRequest) -> ProviderResponse:
        raise NotSupported("voyage provides embeddings only")

    async def embed(
        self, texts: list[str], model: str, *, input_type: str = "document", dimensions: int = 1024
    ) -> EmbedResponse:
        body = {"model": model, "input": texts, "input_type": input_type, "output_dimension": dimensions}
        try:
            resp = await self.client.post(
                f"{self.base}/embeddings", json=body, headers={"authorization": f"Bearer {self.config.api_key or ''}"}
            )
        except httpx2.TransportError as exc:
            raise ProviderError(str(exc), retryable=True) from exc
        if resp.status_code >= 400:
            raise ProviderError(
                resp.text[:500], retryable=resp.status_code >= 500 or resp.status_code == 429, status=resp.status_code
            )
        data = resp.json()
        vectors = [fit_dimensions(d["embedding"], dimensions) for d in sorted(data["data"], key=lambda d: d["index"])]
        return EmbedResponse(
            provider=self.config.name,
            model=data.get("model", model),
            dimensions=dimensions,
            vectors=vectors,
            usage=Usage(input_tokens=data.get("usage", {}).get("total_tokens", 0)),
        )

    async def aclose(self) -> None:
        await self.client.aclose()
