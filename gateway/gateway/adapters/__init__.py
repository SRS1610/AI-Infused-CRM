"""Provider adapters, built from provider configurations."""

from __future__ import annotations

from typing import Any

from gateway.adapters.anthropic import AnthropicAdapter
from gateway.adapters.base import Adapter, Capabilities, Chunk, ProviderRequest, ProviderResponse
from gateway.adapters.google import GoogleAdapter
from gateway.adapters.mock import MockAdapter
from gateway.adapters.openai import AzureOpenAIAdapter, OpenAIAdapter, OpenAICompatibleAdapter
from gateway.adapters.voyage import VoyageAdapter
from gateway.config import ProviderConfig

ADAPTERS: dict[str, type[Adapter]] = {
    "anthropic": AnthropicAdapter,
    "openai": OpenAIAdapter,
    "azure_openai": AzureOpenAIAdapter,
    "openai_compatible": OpenAICompatibleAdapter,
    "google": GoogleAdapter,
    "voyage": VoyageAdapter,
    "mock": MockAdapter,
}


def build_adapter(config: ProviderConfig, http_client: Any = None) -> Adapter:
    return ADAPTERS[config.kind](config, http_client)


__all__ = [
    "ADAPTERS",
    "Adapter",
    "Capabilities",
    "Chunk",
    "ProviderRequest",
    "ProviderResponse",
    "build_adapter",
]
