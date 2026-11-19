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

### Sales and products (sub-phase 2.6)

| Feature | Risk class | Model tier | Writes / outbound | Data used | Limitations |
|---|---|---|---|---|---|
| `sale.upsell` — upsell / cross-sell suggestions | minimal | none (co-purchase association rules in-process; optional small-tier explanation) | add line → write proposal | the company's confirmed orders of the last two years (aggregate counts only), the catalogue's optional products and accessories; products filtered by the user's access | learns only from Tessera orders; cold start falls back to catalogue options and popularity; back-test on synthetic history (top-3 precision 0.78, popularity baseline 0.30) |
| `sale.discount_suggest` — discounts within approved bands | limited | none (logistic win model per category, quantity ramp; optional small-tier explanation) | write proposal per line, never outside the band (clamp + re-check) | discount bands, won/lost quotations of the category (aggregate), product cost (computation only, never shown) | needs ≥ 8 decided lines with both outcomes per category for the history model; acceptance rate is reported from proposal decisions |
| `sale.quote_from_text` — quote lines from pasted text | minimal | small (extract) | write proposal (draft lines) | the pasted text only; catalogue matching runs in Tessera as the user (code, name trigrams, gateway embeddings) | unmatched or ambiguous items are flagged, never invented; a planted instruction that the model turns into a real catalogue item still becomes a proposed line the user must reject; replay scores measure the pipeline (hand-authored extractions) |
| `sale.pricing_breach` — pricing-policy flags | limited | none (rules + customer statistics; optional small-tier summary) | notification / flag (no proposal) | quotation lines, bands, pricelist prices, product cost (computation only), the customer's confirmed orders (aggregate) | "unusual for the customer" ignores volume deals (the two labelled false positives of the eval set); flags are stored only when a quotation is sent, confirmed or checked on request |
| `product.description_draft` — sales description drafts | minimal | mid (draft) | write proposal (`description_sale`, user's language) | the product's catalogue facts read as the user | the spec check covers numbers and a list of common claims in English, French and German; judge scores in replay are hand-authored (D-058) |

### Accounting (`account`)

| Feature | Risk class | Model tier (task class) | Writes / outbound | Data used | Evaluation (replay, `tessera ai eval run --module account`) | Limitations |
|---|---|---|---|---|---|---|
| `account.bill_ocr` — invoice and bill reading with auto-coding | limited | mid (`vision`); digital PDFs need no model | one **action proposal** per document (draft + new vendor); nothing written before acceptance | the uploaded document; partners, bank accounts, products, accounts, taxes and the partner's posted invoice lines **the user can read**; `redaction: minimal` (a page image cannot be redacted, so only PII-approved providers) | 125 synthetic bills (EN/FR/DE/ES, 4 layouts, 84 digital / 35 scans / 6 image-only PDFs, 8 customer invoices, 2 credit notes, 6 prompt injections, 3 isolation cases): header F1 0.998, line F1 0.994, account / tax accuracy 0.992 / 1.0 (both: 0.992), proposal gate 1.0, isolation 1.0 | the corpus is synthetic and the parser was tuned on its layouts; scans are **replayed hand-authored answers** (the gold reading with 7 typical slips), so their scores measure the pipeline — matching, coding, checks, proposal — not a vision model; real documents will need the model and corrections more often |
| `account.anomaly` — anomaly flags on bills | limited | small (`classify`), only for the optional explanation; detection is deterministic | flags + notification (BUILD_PLAN: notification / flag); no proposal; never blocks posting | the vendor's bills, bank accounts and the company's bill amounts of the last year, read as the user (the background job acts for the bill's company) | 127 labelled cases: duplicate recall 0.968, false-positive rate 0.013 (one legitimately repeated order is flagged; one hand-re-keyed copy a cent off, 14 days later with a new number, is missed) | facts-level evaluation: the ledger adapter and the flags are covered by tests, not by the set; cross-vendor duplicates (the same document filed under two vendors) are not looked for |
| `account.cashflow_forecast` — cash-flow forecast | limited | mid (`summarize`) for the narrative only; figures from the engine | none | bank and outstanding accounts, open invoices / bills, paid documents (payment delays), bank movements not linked to invoices, read as the user | back-test on 120 simulated 9-month histories, 8 weeks after the cut-off: MAPE of the weekly position 0.085 (median 0.053; flat-balance baseline 0.164); narratives: numbers from the engine 1.0 (14 hand-authored narratives with a wrong or injected figure rejected) | MAPE explodes when the actual balance is near zero; no bank-statement lines not yet booked, no planned taxes, loans or investments; narratives are replayed |

Numbers never come from a model: the bill reader keeps a model's amounts only as a *reading* that is
re-checked against the tax engine and, for PDFs, against the text layer; the anomaly explanation and the
forecast narrative are replaced by a summary of the facts when they quote a number that is not in them.

### Subscriptions, rentals and commissions (sub-phase 2.6)

| Feature | What it does | Risk class | Model tier | Data used | Writes / outbound | Evaluation (replay) | Limitations |
|---|---|---|---|---|---|---|---|
| `sale_subscription.churn_predict` | Churn score, risk level and top-5 factors per running subscription; *At risk* filter | limited | in-process logistic regression (per company, prior weights at cold start); optional mid-tier narration | `churn_features` of the subscription (tenure, MRR and changes, pauses, failed payments, invoices, customer e-mail count); never its state, close reason or customer identity | Score fields only (daily job / *Score now*); no proposals | AUC 0.86 on 120 held-out synthetic subscriptions; explanations 100 %; factor validity 1.0 | Trained on ended vs surviving subscriptions with lifetime counters (no point-in-time snapshots); the synthetic back-test and the prior share the same domain assumptions |
| `sale_subscription.renewal_draft` | Renewal e-mail from plan, products, tenure, MRR history, renewal quotation and churn risk | limited | mid (draft) | The subscription's records as the requesting user; contact name (redaction `minimal`) | Message proposal; nothing posted or queued before *Send* | Judge 4.3/5, numeric consistency 1.0, guard accuracy 1.0, tone match 1.0, 0 messages / mails before acceptance (32 cases) | Cassettes are hand-authored (no live model evaluated); numbers in names are not facts |
| `sale_rental.demand_forecast` | Units out per day / week for the next weeks with an 80 % range, beside what is booked | minimal | in-process statistics; optional mid-tier narrative | The company's own reservations of the product (26 weeks) | None | Weekly MAPE 9.5 % on 120 generated histories (daily 26 %), range coverage 85 % | No holidays or events calendar; new products forecast their short-history average |
| `sale_rental.usage_anomaly` | Alerts: repeated late returns, unusually long bookings, utilisation spikes / drops vs forecast | limited | in-process rules; optional small-tier narration | The company's rentals of the last 180 days and daily utilisation | Alerts and notifications only (notification feature) | Precision 0.96, recall 0.90 on 150 labelled cases | Fixed thresholds per company; one alert per subject per 30 days |
| `sale_commission.explain` | Plain-language explanation of a commission statement | limited | mid (summarize) | `statement_facts` of a statement the user can read (salespeople: their own) | None | Numeric consistency 1.0, guard accuracy 1.0, judged clarity 4.6/5 (32 cases); demo statements checked in the test suite | Cassettes are hand-authored; names and the adjustment note are never counted as numbers |

### Finance (sub-phase 2.6)

Bank, reports, dunning, budgets, assets, tax returns, analytic accounting, e-invoicing and payments. Every feature has
a per-company switch (off → affordance hidden, `404 feature_disabled`, no gateway call, no run), reads as the
requesting user, and writes an `ai_core.run` per request; numbers in generated text are checked against the facts
given (`tessera.ai.facts`). Scores are replayed with `tessera ai eval run <feature>` (deterministic features are
measured for real; model answers and judge grades are hand-authored cassettes, D-058).

| Feature | Risk class | Model tier (task class) | Writes / outbound | Data used | Evaluation (replay) | Limitations |
|---|---|---|---|---|---|---|
| `account_bank.reconcile_suggest` — reconciliation suggestions | minimal | small (`classify`), only to break ties between candidates within 0.05; ranking deterministic | **action proposal** per transaction (`account_bank.match`); nothing matched before acceptance | the transaction, open items the user can read, the company's reconciled counterparties (partner ids only, elevated) | 120 cases (typos, bare serial numbers, fees deducted, partner only in history, groups, ambiguous labels, injections): top-1 accuracy 1.0, top-3 recall 1.0, 0 mutations before acceptance, model asked in 27 cases | the score weights are fixed (tuned on a development seed); multi-currency groups are not combined |
| `account_reports.nl_report` — natural-language reports | minimal | small (`nl_query`) | none: report parameters for review; figures always from the report engine | the question, the report catalogue the user may open, partners / journals the user can read, the user's companies | 58 questions (periods, comparisons, names, unreadable names, injections): parameter accuracy 0.948, field accuracy 0.993, numbers from the model 0 | one report per question; dates the model writes are kept only when typed in the question; replayed readings are hand-authored |
| `account_dunning.message_draft` — personalised reminders | limited | mid (`draft`), `redaction: minimal` (PII-approved providers) | **write proposal** on the reminder (subject, message); no mail before the run is sent | the reminder's ledger facts (amounts, invoices, days late, fee) and the customer's 12-month history | 32 ledger scenarios: amounts correct 1.0, guard 1.0, tone 1.0, judge 0.86, 0 mails before acceptance | the invoice table is always the ledger's; replayed drafts are hand-authored |
| `account_budget.variance_explain` — variance explanations | minimal | mid (`summarize`) | none | `variance_report` of the budget, recomputed as the user | 32 budgets: numeric consistency 1.0, guard 1.0, judge 0.93 | no causes beyond the report; replayed explanations are hand-authored |
| `account_asset.classify` — asset classification | minimal | small (`classify`), only for close calls (two best models within 0.15), answer limited to the offered models | **action proposal**: creates the draft asset from the posted bill line with the model, or sets the model on its draft asset | the bill line (label, product, account), the company's asset models and earlier assets' words | 114 bill lines (EN/FR/DE, close calls, 6 injections): accuracy 0.965, 0 assets created before acceptance, model asked in 7 cases | lexicon in English, French and German; a line naming two kinds of items goes to the model or the first match |
| `account_asset.depreciation_check` — depreciation anomaly checks | minimal | none for the checks; small (`classify`) for the optional explanation | flags only (no proposal) | the asset's board, model and peers (same model) | 61 assets (sound boards and 13 kinds of seeded anomaly, 4 injections): precision 1.0, recall 1.0, expected code found 1.0 | type ranges are fixed defaults; spikes only on straight-line boards |
| `account_tax.prefiling` — pre-filing anomaly checks | limited | none for the checks; small (`classify`) for the optional briefing | flags only, shown on the open tax return; closing never blocked | the period's documents and tax report, the previous four periods (company scope, elevated aggregates) | 64 five-quarter scenarios (13 anomaly kinds x 4 seeds, 8 clean, 4 injections): recall 1.0 (seeded anomaly in the top 3: 1.0), clean-quarter statistical flags 0.0, every finding explained | rates compared, not tax codes; no seasonality beyond four periods; the evaluation's scenarios are synthetic (one company, French chart) |
| `account_analytic.allocation_suggest` — analytic allocation suggestions | minimal | none for the suggestion; small (`classify`) for the optional explanation | **write proposal** on `analytic_distribution` | distribution models, posted invoice / bill lines of the last two years the user can read | 54 six-month histories (8 house rules with 12 % noise, models, cold starts, posted lines, 30 % noise, 4 injections): exact-distribution accuracy 0.944, per-plan 0.955, 0 writes before acceptance | cold start: a partner or product never seen gets only the models' plans; a new kind of purchase from a known vendor gets the vendor's habit |
| `account_einvoice.error_explain` — e-invoice error explanations | minimal | mid (`summarize`) for the words; field attribution deterministic | none | the document's validation findings and the invoice's totals | 40 documents carrying the findings the vendored validators emit on defective invoices (75 labelled rules, 3 injections, 3 probes): field attribution 1.0, links 1.0, guard 1.0, judge 0.955 | catalogue covers the rules seen on Tessera documents; others reported on the whole document fall back to "lines and totals"; labels and catalogue share an author; replayed explanations are hand-authored |
| `payment.failure_summary` — payment failure summary | minimal | none for the categories; mid (`summarize`) for the optional narrative | none | failed / cancelled transactions the user can read (`state_message`), amounts, partners, providers | 63 messages (Stripe decline codes and messages, Tessera messages, FR/DE/ES/IT/NL/PT, 6 hold-out phrasings, 4 injections): category accuracy 0.921 (every category's recall >= 0.89) | phrasings outside the lexicon fall back to *other* (5 of 6 hold-out messages) |

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
