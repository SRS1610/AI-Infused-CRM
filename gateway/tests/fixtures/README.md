# Adapter contract fixtures

Response bodies served to the provider adapters by `tests/test_adapter_contract.py` through an
in-process `httpx2.MockTransport`. One directory per adapter kind:

| directory            | wire format                                                                                   |
|----------------------|-----------------------------------------------------------------------------------------------|
| `anthropic/`         | Messages API JSON; SSE `message_start` / `content_block_*` / `message_delta` / `message_stop`   |
| `openai/`            | Chat Completions JSON; `data:` chunk stream with a final usage chunk and `data: [DONE]`; embeddings |
| `azure_openai/`      | as `openai/`, plus Azure's `prompt_filter_results` / `content_filter_results`                   |
| `openai_compatible/` | as `openai/`, in vLLM's dialect (no cached-token details, 768-dim native embeddings)           |
| `google/`            | Gemini `generateContent` JSON, `streamGenerateContent?alt=sse` stream, `batchEmbedContents`     |
| `voyage/`            | Voyage `/v1/embeddings` JSON                                                                  |

Each directory holds `chat`, `stream`, `tool_use`/`tool_call`, `tool_followup`, `structured`,
`embeddings` (where the provider has them) and `errors/<status>.json` in the provider's error shape.
The test's handler picks the file from the request (endpoint, `stream`, tools, tool result, schema),
and `[[fail:NNN]]` / `[[refuse]]` markers in the request text select the error / refusal bodies.

**These are hand-authored** from each provider's documented wire format, not captured traffic. The
nightly live contract run is meant to replace them with real recordings; keep the file names and the
answer texts the tests assert on (or update the tests alongside).
