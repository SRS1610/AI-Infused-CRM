# AI governance

How Tessera uses AI, what data goes where, how features are classified, and what the platform
enforces. This page describes the platform delivered in Phase 1.5; each later phase adds its
features to the generated classification table (`docs/ai-governance/CLASSIFICATION.md`).

## Principles the code enforces

| Principle | How it is enforced | Tested by |
|---|---|---|
| The AI never sees or changes more than the user | Tools run with the requesting user's environment (access rights, record rules, field groups). Retrieval joins the user's accessible-record query before ranking. | `modules/ai_core/tests/test_runtime.py`, `test_retrieval_evals.py` (200 adversarial queries) |
| AI proposes, a person decides | Write tools only create `ai_core.proposal` rows. Accepting applies the change as the user, through the ORM, in a fresh check. Proposals expire after 7 days and are never auto-applied. | proposal lifecycle and injection-corpus tests |
| Company content is data, never instructions | Every prompt ends with an instruction hierarchy; records are wrapped in escaped `<source>` envelopes; tools are whitelisted per feature. | 50-case prompt-injection corpus (`ai_test.summarize`) |
| Everything is audited | `ai_core.run` (who, company, feature, prompt version, provider, model, tokens, cost, latency, outcome, decision), `ai_core.tool_call`, `ai_core.proposal`, `ai_core.citation`, and the gateway ledger (`ai_gateway.ledger`). | runtime and API tests |
| Degrade to manual | Gateway or provider down → AI affordances show "AI unavailable"; every business workflow works without AI. | `test_gateway_down_*` (backend and client) |

## Data flow

1. The application renders the prompt from the feature's active prompt version and the records the
   user may read.
2. The gateway checks kill switches, residency and budgets, redacts personal data when the feature
   allows it, then calls the provider configured for the company and task class.
3. The answer is validated (JSON Schema, one repair attempt), personal data is restored, the output
   is screened (credential requests, commands, unknown links, leaked placeholders) and every call is
   written to the ledger.

**Personal data.** Features declare `redaction`:

- `redact` (default) replaces e-mail addresses, phone numbers, IBANs, card numbers, social security
  numbers and the names of people present in the context with placeholders before the request leaves
  the deployment.
- `minimal` sends values the feature needs (for example the recipient of a draft) and is only routed
  to providers an administrator marked **approved for personal data**.

Embeddings carry record text verbatim, so they are only computed by approved providers.

**Residency.** Each provider has a region label. A company may restrict itself to some regions; the
gateway then refuses other providers (`policy_refused`) instead of falling back to them.

**Retention.** Prompt inputs and outputs of runs are kept for the company's retention period
(default 90 days, `ai_core.company_setting.retention_days`) and then purged nightly. The audit skeleton
is kept: who, which feature, cost and the decision. Provider-side retention is recorded per provider
(`retention`) and shown to administrators. Anthropic does not train on API data by default and keeps
it 30 days; self-hosted providers keep nothing outside the deployment.

## Model choices

Routing is per company and task class (`ai_core.route`). The defaults follow D-016:

| Task class | Tier | Anthropic model |
|---|---|---|
| classify, extract, translate, nl_query | small | `claude-haiku-5-5` |
| summarize, draft, vision | mid | `claude-sonnet-5-5` |
| reason | large | `claude-opus-5-5` |
| embed | embedding model | Voyage / OpenAI / self-hosted (Anthropic has no embeddings API) |

Effort is set per task class (`low` for classification and extraction, `medium` for drafting and
summaries, `high` for reasoning). On Opus 5.5 and Sonnet 5.5 the Anthropic adapter enables the
provider's server-side fallback for policy refusals; outages fall back to the route's second provider
after three consecutive failures open the circuit.

## Risk classification

Every feature declares `risk_class` (minimal, limited, high) and `employment_related` in its
module's `ai/features.yaml`. `tessera ai classification` generates
`docs/ai-governance/CLASSIFICATION.md` from those manifests, so the table cannot drift from the code.

| Class | Platform obligations |
|---|---|
| Minimal | disclosure label, audit, evaluation gate |
| Limited | the above, explanation payload, opt-out per company (kill switch) |
| High (employment) | the above, linked bias-test results (loading refuses the manifest otherwise), a named human decision, no automated action |

Prohibited uses (emotion recognition at work, social scoring, biometric categorisation) are not
built.

## Evaluation

Each feature has a golden set (`ai/evals/<feature>.jsonl`, at least 30 cases, 100 for scoring and
forecasting) with thresholds in `ai/features.yaml`. CI replays recorded responses
(`ai/cassettes/<feature>.jsonl`) and fails when a threshold is not met; `tessera ai eval report --strict`
fails when any declared feature lacks a prompt, flag, thresholds, cases, cassettes or prompt-injection
cases. Evaluation runs only see their own corpus (the case records and the module records the cases name):
the target database's business data is hidden for the duration of the run, which is rolled back, so the
scores do not depend on the database they run against.

## Feature inventory (Phase 2)

### Accounting (`account`)

| Feature | Risk class | Model tier (task class) | Writes / outbound | Data used | Evaluation (replay, `tessera ai eval run --module account`) | Limitations |
|---|---|---|---|---|---|---|
| `account.bill_ocr` — invoice and bill reading with auto-coding | limited | mid (`vision`); digital PDFs need no model | one **action proposal** per document (draft + new vendor); nothing written before acceptance | the uploaded document; partners, bank accounts, products, accounts, taxes and the partner's posted invoice lines **the user can read**; `redaction: minimal` (a page image cannot be redacted, so only PII-approved providers) | 125 synthetic bills (EN/FR/DE/ES, 4 layouts, 84 digital / 35 scans / 6 image-only PDFs, 8 customer invoices, 2 credit notes, 6 prompt injections, 3 isolation cases): header F1 0.998, line F1 0.994, account / tax accuracy 0.992 / 1.0 (both: 0.992), proposal gate 1.0, isolation 1.0 | the corpus is synthetic and the parser was tuned on its layouts; scans are **replayed hand-authored answers** (the gold reading with 7 typical slips), so their scores measure the pipeline — matching, coding, checks, proposal — not a vision model; real documents will need the model and corrections more often |
| `account.anomaly` — anomaly flags on bills | limited | small (`classify`), only for the optional explanation; detection is deterministic | flags + notification (BUILD_PLAN: notification / flag); no proposal; never blocks posting | the vendor's bills, bank accounts and the company's bill amounts of the last year, read as the user (the background job acts for the bill's company) | 127 labelled cases: duplicate recall 0.968, false-positive rate 0.013 (one legitimately repeated order is flagged; one hand-re-keyed copy a cent off, 14 days later with a new number, is missed) | facts-level evaluation: the ledger adapter and the flags are covered by tests, not by the set; cross-vendor duplicates (the same document filed under two vendors) are not looked for |
| `account.cashflow_forecast` — cash-flow forecast | limited | mid (`summarize`) for the narrative only; figures from the engine | none | bank and outstanding accounts, open invoices / bills, paid documents (payment delays), bank movements not linked to invoices, read as the user | back-test on 120 simulated 9-month histories, 8 weeks after the cut-off: MAPE of the weekly position 0.085 (median 0.053; flat-balance baseline 0.164); narratives: numbers from the engine 1.0 (14 hand-authored narratives with a wrong or injected figure rejected) | MAPE explodes when the actual balance is near zero; no bank-statement lines not yet booked, no planned taxes, loans or investments; narratives are replayed |

Numbers never come from a model: the bill reader keeps a model's amounts only as a *reading* that is
re-checked against the tax engine and, for PDFs, against the text layer; the anomaly explanation and the
forecast narrative are replaced by a summary of the facts when they quote a number that is not in them.

## Known limitations (Phase 1.5)

- **No live model has been evaluated yet.** The cassettes are hand-authored responses in each
  provider's format, so replay runs measure the platform (validation, citations, access control,
  proposals), not model quality. The nightly live run (`tessera ai eval run --mode live`) needs
  provider keys and is not scheduled in CI yet.
- **Rule-based redaction.** Names are only redacted when they are known to be in the context (people
  and companies linked to the record, fields flagged `pii`, honorific-prefixed names). A name typed
  free-form into a description is not detected. A local NER model is a later option (D-048).
- **Speech-to-text and OCR endpoints** answer `not_supported` until the phases that need them.
- **Circuit breakers** are per gateway process; with several gateway replicas each opens its own.
