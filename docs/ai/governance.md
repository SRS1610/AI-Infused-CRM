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

## Feature inventory

### Subscriptions, rentals and commissions (sub-phase 2.6)

| Feature | What it does | Risk class | Model tier | Data used | Writes / outbound | Evaluation (replay) | Limitations |
|---|---|---|---|---|---|---|---|
| `sale_subscription.churn_predict` | Churn score, risk level and top-5 factors per running subscription; *At risk* filter | limited | in-process logistic regression (per company, prior weights at cold start); optional mid-tier narration | `churn_features` of the subscription (tenure, MRR and changes, pauses, failed payments, invoices, customer e-mail count); never its state, close reason or customer identity | Score fields only (daily job / *Score now*); no proposals | AUC 0.86 on 120 held-out synthetic subscriptions; explanations 100 %; factor validity 1.0 | Trained on ended vs surviving subscriptions with lifetime counters (no point-in-time snapshots); the synthetic back-test and the prior share the same domain assumptions |
| `sale_subscription.renewal_draft` | Renewal e-mail from plan, products, tenure, MRR history, renewal quotation and churn risk | limited | mid (draft) | The subscription's records as the requesting user; contact name (redaction `minimal`) | Message proposal; nothing posted or queued before *Send* | Judge 4.3/5, numeric consistency 1.0, guard accuracy 1.0, tone match 1.0, 0 messages / mails before acceptance (32 cases) | Cassettes are hand-authored (no live model evaluated); numbers in names are not facts |
| `sale_rental.demand_forecast` | Units out per day / week for the next weeks with an 80 % range, beside what is booked | minimal | in-process statistics; optional mid-tier narrative | The company's own reservations of the product (26 weeks) | None | Weekly MAPE 9.5 % on 120 generated histories (daily 26 %), range coverage 85 % | No holidays or events calendar; new products forecast their short-history average |
| `sale_rental.usage_anomaly` | Alerts: repeated late returns, unusually long bookings, utilisation spikes / drops vs forecast | limited | in-process rules; optional small-tier narration | The company's rentals of the last 180 days and daily utilisation | Alerts and notifications only (notification feature) | Precision 0.96, recall 0.90 on 150 labelled cases | Fixed thresholds per company; one alert per subject per 30 days |
| `sale_commission.explain` | Plain-language explanation of a commission statement | limited | mid (summarize) | `statement_facts` of a statement the user can read (salespeople: their own) | None | Numeric consistency 1.0, guard accuracy 1.0, judged clarity 4.6/5 (32 cases); demo statements checked in the test suite | Cassettes are hand-authored; names and the adjustment note are never counted as numbers |

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
