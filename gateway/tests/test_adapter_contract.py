"""Shared adapter contract (acceptance criterion 1.5-a).

Every adapter kind runs the same cases where its ``capabilities()`` has the feature: chat, streaming,
tool call (plus the tool-result follow-up), structured output, embeddings and error mapping.

Provider adapters talk to an in-process ``httpx2.MockTransport`` that serves the wire-format fixtures in
``tests/fixtures/<kind>/`` and records every request, so the tests also pin down what each adapter
sends. The mock adapter has no wire protocol and runs live.

Fixture selection is derived from the request itself (embeddings endpoint, ``stream``, a tool result
in the conversation, tools offered, a JSON schema requested), so a fixture is only served when the
adapter actually used the provider's mechanism for that feature. ``[[fail:NNN]]`` anywhere in the
request answers with the provider's error body for that status; ``[[refuse]]`` serves a refusal.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import openai
import pytest

from gateway.adapters import build_adapter
from gateway.adapters.base import Adapter, Chunk, ProviderRequest, ProviderResponse, fit_dimensions
from gateway.adapters.mock import MockAdapter, hash_embedding
from gateway.config import ProviderConfig
from gateway.envelope import EMBEDDING_DIMENSIONS, Block, Message, Stop, ToolSpec, Usage
from gateway.errors import ProviderError

FIXTURES = Path(__file__).parent / "fixtures"
FAIL = re.compile(r"\[\[fail:(\d{3})\]\]")
OPENAI_KINDS = ("openai", "azure_openai", "openai_compatible")
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM = "You are Tessera's CRM assistant. Answer briefly."
CHAT_PROMPT = "Summarise the latest note on the Acme Corp account in one sentence."
TOOL_PROMPT = "Who owns the Acme Corp account?"
TOOL_RESULT = '{"id": 42, "name": "Acme Corp", "tier": "enterprise", "owner": "Dana Kim"}'
EMBED_TEXTS = ["Acme Corp renewed its contract", "Globex asked for a demo", "Initech churned in March"]

LOOKUP = ToolSpec(
    name="lookup_account",
    description="Find a CRM account by name.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Account name"}},
        "required": ["name"],
        "additionalProperties": False,
    },
)
SEARCH = ToolSpec(  # open schema: must not be declared strict
    name="search_notes",
    description="Full-text search over activity notes.",
    input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
)
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Short label for the note"},
        "sentiment": {"type": "string", "enum": ["positive", "neutral", "negative"]},
        "next_step": {
            "type": "object",
            "properties": {"action": {"type": "string"}, "due": {"type": "string"}},
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    "required": ["title", "sentiment", "next_step"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------------------- fixture server


@dataclass
class Recorded:
    method: str
    url: httpx2.URL
    headers: httpx2.Headers
    body: Any

    @property
    def text(self) -> str:
        return json.dumps(self.body)


class FixtureServer:
    """Serves ``tests/fixtures/<kind>/`` and records the requests the adapter made."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.dir = FIXTURES / kind
        self.requests: list[Recorded] = []

    @property
    def last(self) -> Recorded:
        assert self.requests, "the adapter sent no request"
        return self.requests[-1]

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        rec = Recorded(request.method, request.url, request.headers, body)
        self.requests.append(rec)
        failed = FAIL.search(rec.text)
        if failed:
            status = int(failed.group(1))
            return httpx2.Response(
                status, content=(self.dir / "errors" / f"{status}.json").read_bytes(), headers=_JSON, request=request
            )
        name = self.scenario(request.url.path, body or {}, rec.text)
        content = (self.dir / name).read_bytes()
        headers = _SSE if name.endswith(".sse") else _JSON
        return httpx2.Response(200, content=content, headers=headers, request=request)

    def scenario(self, path: str, body: dict[str, Any], text: str) -> str:
        if self.kind == "anthropic":
            assert path == "/v1/messages", path
            msgs = body.get("messages", [])
            if "[[refuse]]" in text:
                return "refusal.json"
            if body.get("stream"):
                return "stream.sse"
            if any(
                isinstance(m["content"], list) and any(b["type"] == "tool_result" for b in m["content"]) for m in msgs
            ):
                return "tool_followup.json"
            if body.get("tools"):
                return "tool_use.json"
            if (body.get("output_config") or {}).get("format"):
                return "structured.json"
            return "chat.json"
        if self.kind in OPENAI_KINDS:
            if path.endswith("/embeddings"):
                return "embeddings.json"
            assert path.endswith("/chat/completions"), path
            if body.get("stream"):
                return "stream.sse"
            if any(m["role"] == "tool" for m in body["messages"]):
                return "tool_followup.json"
            if body.get("tools"):
                return "tool_call.json"
            if body.get("response_format"):
                return "structured.json"
            return "chat.json"
        if self.kind == "google":
            if path.endswith(":batchEmbedContents"):
                return "embeddings.json"
            if path.endswith(":streamGenerateContent"):
                return "stream.sse"
            assert path.endswith(":generateContent"), path
            if any("functionResponse" in p for c in body["contents"] for p in c["parts"]):
                return "tool_followup.json"
            if body.get("tools"):
                return "tool_call.json"
            if body.get("generationConfig", {}).get("responseSchema"):
                return "structured.json"
            return "chat.json"
        assert self.kind == "voyage" and path == "/v1/embeddings", path
        return "embeddings.json"


_JSON = {"content-type": "application/json"}
_SSE = {"content-type": "text/event-stream; charset=utf-8", "cache-control": "no-cache"}


def http_client(kind: str, server: FixtureServer) -> httpx2.AsyncClient:
    transport = httpx2.MockTransport(server.handler)
    if kind == "anthropic":
        return anthropic.DefaultAsyncHttpxClient(transport=transport)
    if kind in OPENAI_KINDS:
        return openai.DefaultAsyncHttpxClient(transport=transport)
    return httpx2.AsyncClient(transport=transport)


# ---------------------------------------------------------------------------------------- cases


@dataclass
class Case:
    kind: str
    chat_model: str = ""
    embed_model: str = ""
    base_url: str | None = None
    options: dict[str, Any] = field(default_factory=dict)
    # normalised usage of the chat and stream fixtures; None for the live mock (only non-zero is checked)
    chat_usage: Usage | None = None


CASES: dict[str, Case] = {
    "anthropic": Case(
        "anthropic", "claude-sonnet-5-5", chat_usage=Usage(input_tokens=42, output_tokens=21, cache_read_tokens=1800)
    ),
    "openai": Case(
        "openai",
        "gpt-4.1-mini",
        "text-embedding-3-small",
        chat_usage=Usage(input_tokens=1600 - 1536, output_tokens=18, cache_read_tokens=1536),
    ),
    "azure_openai": Case(
        "azure_openai",
        "gpt-4.1-mini",
        "text-embedding-3-small",
        base_url="https://tessera-eu.openai.azure.com",
        options={
            "api_version": "2024-10-21",
            "deployments": {"gpt-4.1-mini": "crm-chat-prod", "text-embedding-3-small": "crm-embed-prod"},
        },
        chat_usage=Usage(input_tokens=1600 - 1536, output_tokens=18, cache_read_tokens=1536),
    ),
    "openai_compatible": Case(
        "openai_compatible",
        "meta-llama/Llama-3.3-70B-Instruct",
        "nomic-ai/nomic-embed-text-v1.5",
        base_url="http://vllm.internal:8000/v1",
        chat_usage=Usage(input_tokens=1600, output_tokens=18),
    ),
    "google": Case(
        "google",
        "gemini-2.5-flash",
        "gemini-embedding-001",
        # output = candidatesTokenCount + thoughtsTokenCount (thinking tokens are billed as output)
        chat_usage=Usage(input_tokens=1650 - 1024, output_tokens=18 + 96, cache_read_tokens=1024),
    ),
    "voyage": Case("voyage", embed_model="voyage-3.5"),
    "mock": Case(
        "mock",
        "mock-mid",
        "mock-embed",
        options={
            "script": [
                {
                    "match": {"contains": TOOL_PROMPT},
                    "respond": {"tool_calls": [{"name": "lookup_account", "input": {"name": "Acme Corp"}}]},
                }
            ]
        },
    ),
}
KINDS = list(CASES)


@dataclass
class Harness:
    case: Case
    adapter: Adapter
    server: FixtureServer | None  # None for the live mock adapter

    @property
    def kind(self) -> str:
        return self.case.kind

    @property
    def last_mock_request(self) -> dict[str, Any]:
        return MockAdapter.captured[-1]


def make_harness(kind: str, **options: Any) -> Harness:
    case = CASES[kind]
    config = ProviderConfig(
        id=KINDS.index(kind) + 1,
        name=f"{kind}-contract",
        kind=kind,
        api_key=f"test-key-{kind}",
        base_url=case.base_url,
        options={"max_retries": 0, **case.options, **options},
    )
    if kind == "mock":
        MockAdapter.reset()
        return Harness(case, build_adapter(config), None)
    server = FixtureServer(kind)
    return Harness(case, build_adapter(config, http_client(kind, server)), server)


@pytest.fixture(params=KINDS)
async def h(request: pytest.FixtureRequest) -> AsyncIterator[Harness]:
    harness = make_harness(request.param)
    yield harness
    await harness.adapter.aclose()


def req(case: Case, prompt: str = CHAT_PROMPT, **kw: Any) -> ProviderRequest:
    kw.setdefault("messages", [Message(role="user", content=prompt)])
    return ProviderRequest(
        model=kw.pop("model", case.chat_model),
        system=SYSTEM,
        max_output_tokens=512,
        metadata={"feature": "contract", "warm_cache": True},
        **kw,
    )


def require(h: Harness, capability: str) -> None:
    if not getattr(h.adapter.capabilities(), capability):
        pytest.skip(f"{h.kind} does not support {capability}")


def assert_usage(h: Harness, usage: Usage) -> None:
    assert usage.input_tokens > 0
    assert usage.output_tokens > 0
    if h.case.chat_usage is not None:
        assert usage == h.case.chat_usage
    else:  # live mock: warm_cache metadata makes it report cache reads
        assert usage.cache_read_tokens > 0


def fixture_json(kind: str, name: str) -> Any:
    return json.loads((FIXTURES / kind / name).read_text())


# ---------------------------------------------------------------------------------------- 1. chat


async def test_chat(h: Harness) -> None:
    require(h, "chat")
    resp = await h.adapter.complete(req(h.case))
    assert isinstance(resp, ProviderResponse)
    assert resp.stop is Stop.end
    assert [b.type for b in resp.content] == ["text"]  # thinking and other blocks carry no answer content
    assert resp.text.strip()
    if h.server:
        assert resp.text == "Acme Corp renewed its annual contract and asked for a pricing review in Q3."
        assert resp.raw_id
    else:
        assert CHAT_PROMPT[:40] in resp.text
    assert resp.model
    assert_usage(h, resp.usage)


# ---------------------------------------------------------------------------------------- 2. streaming


async def test_stream(h: Harness) -> None:
    require(h, "stream")
    chunks: list[Chunk] = [c async for c in h.adapter.stream(req(h.case))]
    texts = [c.text for c in chunks if c.text]
    assert len(texts) > 1, "the answer should arrive in several deltas"
    final = chunks[-1].final
    assert final is not None, "a stream ends with Chunk(final=...)"
    assert all(c.final is None for c in chunks[:-1])
    assert "".join(texts) == final.text
    assert final.stop is Stop.end
    assert_usage(h, final.usage)
    if h.server and h.kind != "google":  # Gemini streams via the :streamGenerateContent?alt=sse endpoint
        assert h.server.last.body["stream"] is True


# ---------------------------------------------------------------------------------------- 3. tool call


def assert_tool_result_sent(h: Harness, call: Block) -> None:
    """The follow-up carries the model's call and our result in the provider's native format."""
    if h.server is None:
        last = h.last_mock_request["messages"][-1]
        assert last["role"] == "user"
        assert last["content"][0]["type"] == "tool_result"
        assert last["content"][0]["tool_use_id"] == call.id
        assert last["content"][0]["content"] == TOOL_RESULT
        return
    body = h.server.last.body
    if h.kind == "anthropic":
        assistant, user = body["messages"][-2:]
        assert assistant["role"] == "assistant"
        assert {
            "type": "tool_use",
            "id": call.id,
            "name": "lookup_account",
            "input": {"name": "Acme Corp"},
        } in assistant["content"]
        assert user == {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": call.id, "content": TOOL_RESULT, "is_error": False}],
        }
    elif h.kind in OPENAI_KINDS:
        assistant, tool = body["messages"][-2:]
        assert assistant["role"] == "assistant"
        (tc,) = assistant["tool_calls"]
        assert tc["id"] == call.id and tc["type"] == "function"
        assert tc["function"]["name"] == "lookup_account"
        assert json.loads(tc["function"]["arguments"]) == {"name": "Acme Corp"}
        assert tool == {"role": "tool", "tool_call_id": call.id, "content": TOOL_RESULT}
    elif h.kind == "google":
        model_turn, user_turn = body["contents"][-2:]
        assert model_turn == {
            "role": "model",
            "parts": [{"functionCall": {"name": "lookup_account", "args": {"name": "Acme Corp"}}}],
        }
        assert user_turn == {
            "role": "user",
            "parts": [{"functionResponse": {"name": "lookup_account", "response": {"result": TOOL_RESULT}}}],
        }
    else:  # pragma: no cover
        raise AssertionError(h.kind)


async def test_tool_call(h: Harness) -> None:
    require(h, "tools")
    first = await h.adapter.complete(req(h.case, TOOL_PROMPT, tools=[LOOKUP, SEARCH]))
    assert first.stop is Stop.tool_use
    calls = [b for b in first.content if b.type == "tool_use"]
    assert len(calls) == 1
    call = calls[0]
    assert call.id
    assert call.name == "lookup_account"
    assert isinstance(call.input, dict)
    assert call.input == {"name": "Acme Corp"}
    assert first.usage.input_tokens > 0 and first.usage.output_tokens > 0

    followup = [
        Message(role="user", content=TOOL_PROMPT),
        Message(role="assistant", content=first.content),
        Message(role="user", content=[Block(type="tool_result", tool_use_id=call.id, content=TOOL_RESULT)]),
    ]
    second = await h.adapter.complete(req(h.case, tools=[LOOKUP, SEARCH], messages=followup))
    assert_tool_result_sent(h, call)
    assert second.stop is Stop.end
    assert second.text.strip()
    assert not [b for b in second.content if b.type == "tool_use"]


# ---------------------------------------------------------------------------------------- 4. structured output


def strip_additional_properties(schema: Any) -> Any:
    """What Gemini's OpenAPI-subset ``responseSchema`` should receive (property names are kept as-is)."""
    if isinstance(schema, dict):
        return {
            k: {n: strip_additional_properties(p) for n, p in v.items()}
            if k == "properties"
            else strip_additional_properties(v)
            for k, v in schema.items()
            if k != "additionalProperties"
        }
    if isinstance(schema, list):
        return [strip_additional_properties(v) for v in schema]
    return schema


def assert_schema_sent(h: Harness) -> None:
    if h.server is None:
        assert h.last_mock_request["output_schema"] == SCHEMA
        return
    body = h.server.last.body
    if h.kind == "anthropic":
        assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    elif h.kind in OPENAI_KINDS:
        assert body["response_format"] == {
            "type": "json_schema",
            "json_schema": {"name": "output", "schema": SCHEMA, "strict": True},
        }
    elif h.kind == "google":
        gen = body["generationConfig"]
        assert gen["responseMimeType"] == "application/json"
        assert gen["responseSchema"] == strip_additional_properties(SCHEMA)
        assert "additionalProperties" not in json.dumps(gen["responseSchema"])
        assert set(gen["responseSchema"]["properties"]) == {"title", "sentiment", "next_step"}
    else:  # pragma: no cover
        raise AssertionError(h.kind)


async def test_structured_output(h: Harness) -> None:
    require(h, "structured_output")
    resp = await h.adapter.complete(req(h.case, "Classify the latest note on Acme Corp.", output_schema=SCHEMA))
    assert_schema_sent(h)
    assert resp.stop is Stop.end
    parsed = json.loads(resp.text)
    assert isinstance(parsed, dict)
    assert {"title", "sentiment", "next_step"} <= set(parsed)


# ---------------------------------------------------------------------------------------- 5. embeddings


def expected_vectors(kind: str) -> list[list[float]]:
    if kind == "mock":
        return [hash_embedding(t, EMBEDDING_DIMENSIONS) for t in EMBED_TEXTS]
    data = fixture_json(kind, "embeddings.json")
    if kind == "google":
        raw = [e["values"] for e in data["embeddings"]]
    else:
        raw = [d["embedding"] for d in sorted(data["data"], key=lambda d: d["index"])]
    return [fit_dimensions(v, EMBEDDING_DIMENSIONS) for v in raw]


async def test_embeddings(h: Harness) -> None:
    require(h, "embeddings")
    resp = await h.adapter.embed(EMBED_TEXTS, h.case.embed_model, input_type="document")
    assert resp.dimensions == EMBEDDING_DIMENSIONS
    assert len(resp.vectors) == len(EMBED_TEXTS)
    assert all(len(v) == EMBEDDING_DIMENSIONS for v in resp.vectors)
    assert resp.vectors == expected_vectors(h.kind)  # input order preserved
    assert len({tuple(v) for v in resp.vectors}) == len(EMBED_TEXTS)
    assert resp.provider == f"{h.kind}-contract"
    if h.server:
        sent = json.dumps(h.server.last.body)
        assert all(json.dumps(t) in sent for t in EMBED_TEXTS)


# ---------------------------------------------------------------------------------------- 6. errors


@pytest.mark.parametrize(("status", "retryable"), [(500, True), (400, False), (429, True)])
@pytest.mark.parametrize("kind", KINDS)
async def test_error_mapping(kind: str, status: int, retryable: bool) -> None:
    h = make_harness(kind, **({"fail_status": status, "fail_times": -1} if kind == "mock" else {}))
    marker = f"[[fail:{status}]]"
    try:
        with pytest.raises(ProviderError) as info:
            if h.adapter.capabilities().chat:
                await h.adapter.complete(req(h.case, f"{CHAT_PROMPT} {marker}"))
            else:
                await h.adapter.embed([*EMBED_TEXTS, marker], h.case.embed_model)
    finally:
        await h.adapter.aclose()
    assert info.value.retryable is retryable
    assert info.value.status == status
    if h.server:
        assert len(h.server.requests) == 1, "max_retries=0: the SDK must not retry"


@pytest.mark.parametrize("kind", [k for k in KINDS if k != "voyage"])
async def test_stream_error_mapping(kind: str) -> None:
    h = make_harness(kind, **({"fail_status": 503, "fail_times": -1} if kind == "mock" else {}))
    try:
        with pytest.raises(ProviderError) as info:
            async for _ in h.adapter.stream(req(h.case, f"{CHAT_PROMPT} [[fail:500]]")):
                pass
    finally:
        await h.adapter.aclose()
    assert info.value.retryable is True


# ---------------------------------------------------------------------------------------- Anthropic specifics


async def test_anthropic_request_shape() -> None:
    h = make_harness("anthropic", inference_geo="us")
    assert h.server
    try:
        await h.adapter.complete(req(h.case, TOOL_PROMPT, tools=[LOOKUP, SEARCH], effort="low"))
    finally:
        await h.adapter.aclose()
    sent = h.server.last
    assert sent.method == "POST"
    # SDK 2.x sends the API key as a bearer token (1.x used x-api-key); either authenticates
    key = "test-key-anthropic"
    assert sent.headers.get("authorization") == f"Bearer {key}" or sent.headers.get("x-api-key") == key
    assert sent.headers["anthropic-version"]
    body = sent.body
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] == 512
    assert body["system"] == [{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
    lookup, search = body["tools"]
    assert lookup["name"] == "lookup_account" and lookup["strict"] is True
    assert lookup["input_schema"] == LOOKUP.input_schema
    assert "strict" not in search  # open schema
    assert search["cache_control"] == {"type": "ephemeral"}  # breakpoint on the last tool only
    assert "cache_control" not in lookup
    assert body["output_config"] == {"effort": "low"}
    assert body["inference_geo"] == "us"
    assert "thinking" not in body and "tool_choice" not in body


async def test_anthropic_no_cache_no_geo_no_effort() -> None:
    h = make_harness("anthropic")
    assert h.server
    try:
        await h.adapter.complete(req(h.case, cache=False))
    finally:
        await h.adapter.aclose()
    body = h.server.last.body
    assert body["system"] == [{"type": "text", "text": SYSTEM}]
    assert "output_config" not in body and "inference_geo" not in body


@pytest.mark.parametrize(("model", "fallback"), [("claude-opus-5-5", True), ("claude-haiku-5-5", False)])
@pytest.mark.parametrize("streaming", [False, True])
async def test_anthropic_server_side_fallback(model: str, fallback: bool, streaming: bool) -> None:
    h = make_harness("anthropic")
    assert h.server
    try:
        if streaming:
            [c async for c in h.adapter.stream(req(h.case, model=model))]
        else:
            await h.adapter.complete(req(h.case, model=model))
    finally:
        await h.adapter.aclose()
    sent = h.server.last
    betas = [b.strip() for b in sent.headers.get("anthropic-beta", "").split(",") if b.strip()]
    assert sent.body["model"] == model
    assert "betas" not in sent.body  # the SDK turns ``betas`` into the header
    if fallback:
        assert FALLBACK_BETA in betas
        assert sent.body["fallbacks"] == "default"
    else:
        assert FALLBACK_BETA not in betas
        assert "fallbacks" not in sent.body


async def test_anthropic_server_fallback_can_be_disabled() -> None:
    h = make_harness("anthropic", server_fallback=False)
    assert h.server
    try:
        await h.adapter.complete(req(h.case, model="claude-opus-5-5"))
    finally:
        await h.adapter.aclose()
    assert "fallbacks" not in h.server.last.body
    assert FALLBACK_BETA not in h.server.last.headers.get("anthropic-beta", "")


async def test_anthropic_refusal() -> None:
    h = make_harness("anthropic")
    try:
        resp = await h.adapter.complete(req(h.case, f"{CHAT_PROMPT} [[refuse]]"))
    finally:
        await h.adapter.aclose()
    assert resp.stop is Stop.refusal
    assert resp.refusal_category == "cyber"
    assert resp.text == ""
    assert resp.usage.input_tokens == 95


async def test_anthropic_inference_geo_reported() -> None:
    h = make_harness("anthropic")
    try:
        resp = await h.adapter.complete(req(h.case))
    finally:
        await h.adapter.aclose()
    assert resp.inference_geo == "global"


# ---------------------------------------------------------------------------------------- OpenAI family specifics


async def test_openai_request_shape() -> None:
    h = make_harness("openai")
    assert h.server
    try:
        await h.adapter.complete(req(h.case, TOOL_PROMPT, tools=[LOOKUP, SEARCH]))
        [c async for c in h.adapter.stream(req(h.case))]
    finally:
        await h.adapter.aclose()
    tool_req, stream_req = h.server.requests
    assert str(tool_req.url) == "https://api.openai.com/v1/chat/completions"
    assert tool_req.headers["authorization"] == "Bearer test-key-openai"
    body = tool_req.body
    assert body["model"] == "gpt-4.1-mini"
    assert body["max_completion_tokens"] == 512
    assert body["messages"][0] == {"role": "system", "content": SYSTEM}
    lookup, search = body["tools"]
    assert lookup == {
        "type": "function",
        "function": {
            "name": "lookup_account",
            "description": LOOKUP.description,
            "parameters": LOOKUP.input_schema,
            "strict": True,
        },
    }
    assert "strict" not in search["function"]
    assert stream_req.body["stream"] is True
    assert stream_req.body["stream_options"] == {"include_usage": True}


async def test_openai_embeddings_request() -> None:
    h = make_harness("openai")
    assert h.server
    try:
        await h.adapter.embed(EMBED_TEXTS, "text-embedding-3-small")
    finally:
        await h.adapter.aclose()
    sent = h.server.last
    assert str(sent.url) == "https://api.openai.com/v1/embeddings"
    assert sent.body["model"] == "text-embedding-3-small"
    assert sent.body["input"] == EMBED_TEXTS
    assert sent.body["dimensions"] == EMBEDDING_DIMENSIONS


async def test_azure_uses_deployment_names() -> None:
    h = make_harness("azure_openai")
    assert h.server
    try:
        await h.adapter.complete(req(h.case))
        [c async for c in h.adapter.stream(req(h.case))]
        await h.adapter.embed(EMBED_TEXTS, "text-embedding-3-small")
    finally:
        await h.adapter.aclose()
    chat, stream, embed = h.server.requests
    for sent in (chat, stream):
        assert sent.url.host == "tessera-eu.openai.azure.com"
        assert sent.url.path == "/openai/deployments/crm-chat-prod/chat/completions"
        assert sent.url.params["api-version"] == "2024-10-21"
        assert sent.headers["api-key"] == "test-key-azure_openai"
        assert sent.body["model"] == "crm-chat-prod"
    assert embed.url.path == "/openai/deployments/crm-embed-prod/embeddings"
    assert embed.url.params["api-version"] == "2024-10-21"
    assert embed.body["dimensions"] == EMBEDDING_DIMENSIONS


async def test_openai_compatible_json_object_mode() -> None:
    h = make_harness("openai_compatible", json_mode="object")
    assert h.server
    try:
        resp = await h.adapter.complete(req(h.case, output_schema=SCHEMA))
    finally:
        await h.adapter.aclose()
    sent = h.server.last
    assert str(sent.url) == "http://vllm.internal:8000/v1/chat/completions"
    assert sent.body["response_format"] == {"type": "json_object"}
    system = sent.body["messages"][0]
    assert system["role"] == "system"
    assert system["content"].startswith(SYSTEM)
    assert json.dumps(SCHEMA) in system["content"]
    assert json.loads(resp.text)["sentiment"] == "positive"


async def test_openai_compatible_feature_flags() -> None:
    h = make_harness("openai_compatible", json_mode="none", supports_tools=False)
    try:
        caps = h.adapter.capabilities()
        assert not caps.tools and not caps.structured_output
        await h.adapter.complete(req(h.case, tools=[LOOKUP], output_schema=SCHEMA))
    finally:
        await h.adapter.aclose()
    assert h.server
    body = h.server.last.body
    assert "tools" not in body and "response_format" not in body
    assert json.dumps(SCHEMA) in body["messages"][0]["content"]


async def test_openai_compatible_embeddings_are_padded() -> None:
    h = make_harness("openai_compatible")
    assert h.server
    try:
        resp = await h.adapter.embed(EMBED_TEXTS, "nomic-ai/nomic-embed-text-v1.5")
    finally:
        await h.adapter.aclose()
    assert "dimensions" not in h.server.last.body  # not every server accepts it
    native = fixture_json("openai_compatible", "embeddings.json")["data"][0]["embedding"]
    assert len(native) == 768
    assert resp.vectors[0][:768] == native
    assert resp.vectors[0][768:] == [0.0] * (EMBEDDING_DIMENSIONS - 768)


# ---------------------------------------------------------------------------------------- Google specifics


async def test_google_request_shape() -> None:
    h = make_harness("google")
    assert h.server
    try:
        await h.adapter.complete(req(h.case, TOOL_PROMPT, tools=[LOOKUP, SEARCH]))
        [c async for c in h.adapter.stream(req(h.case))]
        await h.adapter.embed(EMBED_TEXTS, "gemini-embedding-001", input_type="query")
    finally:
        await h.adapter.aclose()
    tool_req, stream_req, embed_req = h.server.requests
    base = "https://generativelanguage.googleapis.com/v1beta/models"
    assert str(tool_req.url) == f"{base}/gemini-2.5-flash:generateContent"
    assert tool_req.headers["x-goog-api-key"] == "test-key-google"
    assert tool_req.body["systemInstruction"] == {"parts": [{"text": SYSTEM}]}
    assert tool_req.body["contents"] == [{"role": "user", "parts": [{"text": TOOL_PROMPT}]}]
    assert tool_req.body["generationConfig"] == {"maxOutputTokens": 512}
    (decls,) = tool_req.body["tools"]
    lookup, search = decls["functionDeclarations"]
    assert lookup["name"] == "lookup_account"
    assert lookup["parameters"] == {
        "type": "object",
        "properties": {"name": {"type": "string", "description": "Account name"}},
        "required": ["name"],
    }
    assert search["parameters"] == SEARCH.input_schema
    assert str(stream_req.url) == f"{base}/gemini-2.5-flash:streamGenerateContent?alt=sse"
    assert str(embed_req.url) == f"{base}/gemini-embedding-001:batchEmbedContents"
    assert embed_req.body["requests"][0] == {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": EMBED_TEXTS[0]}]},
        "taskType": "RETRIEVAL_QUERY",
        "outputDimensionality": EMBEDDING_DIMENSIONS,
    }


# ---------------------------------------------------------------------------------------- Voyage specifics


async def test_voyage_request_shape() -> None:
    h = make_harness("voyage")
    assert h.server
    try:
        resp = await h.adapter.embed(EMBED_TEXTS, "voyage-3.5", input_type="query")
    finally:
        await h.adapter.aclose()
    sent = h.server.last
    assert str(sent.url) == "https://api.voyageai.com/v1/embeddings"
    assert sent.headers["authorization"] == "Bearer test-key-voyage"
    assert sent.body == {
        "model": "voyage-3.5",
        "input": EMBED_TEXTS,
        "input_type": "query",
        "output_dimension": EMBEDDING_DIMENSIONS,
    }
    assert resp.usage.input_tokens == 19


# ---------------------------------------------------------------------------------------- mock specifics


async def test_mock_captures_what_would_leave_the_deployment() -> None:
    h = make_harness("mock")
    await h.adapter.complete(req(h.case, TOOL_PROMPT, tools=[LOOKUP], effort="high"))
    captured = h.last_mock_request
    assert captured["provider"] == "mock-contract"
    assert captured["system"] == SYSTEM
    assert captured["tools"] == ["lookup_account"]
    assert captured["effort"] == "high"
    assert captured["messages"] == [{"role": "user", "content": TOOL_PROMPT}]
