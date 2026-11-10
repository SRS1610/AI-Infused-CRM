# Building an AI feature

Every AI feature in Tessera is a manifest entry, a prompt file, an evaluation set and a few lines of
Python. Business code never calls a provider: it calls `tessera.ai.run_feature` (or
`stream_feature`), and the platform handles routing, budgets, redaction, validation, audit and the
human decision.

## 1. Declare the feature

`modules/<module>/ai/features.yaml`:

```yaml
features:
  - name: crm.followup_draft          # unique; also the default prompt name
    title: Follow-up email draft
    task_class: draft                 # classify | extract | summarize | draft | reason | translate | nl_query | …
    tools: [records.read, chatter.read_thread]   # whitelist; anything else is refused
    writes: false                     # produces write proposals
    outbound: true                    # produces message proposals
    redaction: minimal                # redact (default) | minimal (needs PII-approved provider) | none
    risk_class: limited               # minimal | limited | high
    employment_related: false
    interactive: true
    cache_ttl: 0                      # seconds a response may be served from cache
    max_steps: 4                      # bound of the tool loop
    eval:
      set: evals/crm.followup_draft.jsonl
      min_cases: 30
      thresholds: {contains: ">= 0.9", injection_resistance: "== 1.0", rubric_score: ">= 4/5"}
    flag: {default: true, rollout: per_company}
```

Loading happens when the module is installed or upgraded (and for every installed module when
`ai_core` is installed): it creates `ai_core.feature` and its default flag.

## 2. Write the prompt

`modules/<module>/ai/prompts/<anything>.yaml`:

```yaml
name: crm.followup_draft
task_class: draft
system: |
  You write short, friendly follow-up e-mails for {{ company.name }}.
template: |
  {{ sources | envelopes }}

  Draft a follow-up to {{ record.partner_id.display_name }}{% if inputs.get('tone') %} in a {{ inputs.get('tone') }} tone{% endif %}.
input_schema: {type: object, properties: {tone: {type: string}}, additionalProperties: false}
output_schema: null                    # a JSON Schema makes the answer structured and validated
```

- Templates are Jinja2 in a sandbox with strict undefined: use `inputs.get('x')` for optional inputs.
- Variables: `inputs`, `sources`, `record`, `user`, `company`, `today`.
- `sources | envelopes` wraps each source in an escaped `<source id="model:id">` element; the platform
  appends the instruction hierarchy to every system prompt.
- A changed file becomes a new prompt version on the next upgrade; active versions are immutable.

## 3. Prepare the context and the output

```python
from tessera.ai import Source
from tessera.ai.runtime import handler, propose_from_output

@handler("crm.followup_draft")
def followup(env, record, inputs):
    record.check_access("read")
    return {
        "sources": [Source(f"crm.lead:{record.id}", record.name, record.description or "")],
        "on_output": propose_from_output("message", "crm.lead", record.id, {}, "Follow-up e-mail"),
    }
```

The API (`POST /api/v1/ai/features/<name>/run` and `/stream`) calls the handler, then the runtime.
From Python: `run_feature(env, "crm.followup_draft", inputs, record=lead, **followup(env, lead, inputs))`.

## 4. Tools

```python
from tessera.ai import ToolContext, tool

@tool("crm.read_lead", "Read one lead the user can access.")
def read_lead(ctx: ToolContext, lead_id: int, fields: list[str] | None = None) -> dict:
    lead = ctx.env["crm.lead"].browse([lead_id])
    lead.check_access("read")
    data = lead.read(fields or ["name", "stage_id"])[0]
    ctx.cite(lead, str(data))          # makes [source:crm.lead:<id>] a valid citation
    return data

@tool("crm.propose_stage", "Propose moving a lead to another stage.", writes=True)
def propose_stage(ctx: ToolContext, lead_id: int, stage: str) -> dict:
    return ctx.propose("transition", "crm.lead", lead_id, payload={"to_state": stage}, title="Move stage")
```

- JSON Schemas come from the type hints (closed objects, so providers validate strictly).
- Every tool call runs in a savepoint that is **rolled back**; `writes=True` tools can only queue
  proposals. Read tools that try to propose are ignored.
- Built-in tools: `records.read`, `records.search`, `chatter.read_thread`, `retrieval.search`,
  `records.propose_write`, `records.propose_create`, `chatter.propose_message`,
  `chatter.propose_activity`.
- Named actions for `action` proposals are registered with `tessera.ai.proposals.action(name)`; nothing
  else can be called.

## 5. Retrieval

Opt a model in with `_ai_index = ["description", "partner_id", ...]`. Records and their chatter
messages are chunked and embedded by background jobs after each change; `retrieval.search(env, query)`
returns only chunks of records the user can read, ranked by vector similarity fused with trigram
similarity.

## 6. Evaluate

A case in `ai/evals/<feature>.jsonl`:

```json
{"id": "c01", "setup": [{"ref": "lead", "model": "crm.lead", "values": {"name": "Acme", "company_id": "$xml:core.main_company"}}],
 "record": "lead", "as_user": "demo", "inputs": {"tone": "warm"},
 "expected": {"contains": ["Acme"], "max_proposals": 1}, "tags": ["injection"]}
```

- `$id:ref` and `{{id:ref}}` / `{{src:ref}}` refer to setup records; `$xml:module.id` to data records.
- Metrics: `schema_valid`, `exact_match`, `field_f1`, `contains`, `citation_validity`,
  `injection_resistance`, `rubric_score`.
- `tessera ai eval run <feature> --mode record` records cassettes against the configured providers;
  CI uses `--mode replay` (deterministic, no keys). `--mode live` runs without recording.
- `tessera ai eval report --strict` lists what any declared feature still lacks.

## 7. Client

- `useAiFeature(name)` → `{ enabled, available }`: hide the affordance when disabled, show
  `<AiUnavailable/>` when the gateway is down.
- `registerAiDraft(model, feature)` adds **Draft with AI** to the chatter composer of that model.
- `<ProposalCard proposal={…}/>` reviews any proposal; `<AiLabel/>` marks AI-generated text until a
  person edits or accepts it; `<Feedback runId={…}/>` records thumbs up/down.

## Local development

- `TESSERA_AI_GATEWAY_URL=inprocess` runs the gateway library inside the API process (tests use it).
- With demo data, a deterministic **Local test provider** is routed for every task class, so every
  feature works without provider keys. Script its answers in the provider's `options.script` (see
  `gateway/gateway/adapters/mock.py`).
