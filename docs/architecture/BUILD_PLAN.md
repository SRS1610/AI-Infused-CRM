# Tessera — Phased Build Plan

**Status:** Draft for approval (pre–Phase 1)
**Companion documents:** [Architecture](ARCHITECTURE.md) · [Module dependency map](MODULE_DEPENDENCY_MAP.md) · [Core ERD](ERD_CORE.md) · [AI architecture note](AI_ARCHITECTURE.md) · [DECISIONS.md](../../DECISIONS.md)

---

## 1. How to read this plan

### 1.1 What is committed

This plan commits to **ordering** and **exit criteria**, not calendar dates. Phases are built in the order below because each depends on the kernel and platform services of the one before it. Within a phase, sub-phases land as separate, reviewable increments; the branch is always runnable at a sub-phase boundary.

### 1.2 Definition of "phase done"

A phase (or sub-phase) is done only when all of the following hold:

1. `docker compose up` from a clean checkout brings up the full stack (Postgres+pgvector, Redis, MinIO, Mailpit, web/API, realtime, worker, scheduler, AI gateway, frontend) and `tessera seed demo` populates the phase's demo scenario.
2. All tests are green in CI: unit, integration, API contract, frontend unit, Playwright e2e, security suites, AI evaluations (replay mode). Coverage ≥ 80% backend and frontend (enforced gate).
3. `tessera db check` reports no schema drift after `tessera db migrate` on (a) an empty database and (b) a database seeded at the previous phase.
4. Documentation is updated: OpenAPI reference regenerated, admin guide and per-module developer guide sections written, `CHANGELOG.md` entry, `DECISIONS.md` entries for every default taken during the phase.
5. A **demo script** (`docs/demos/phase-N.md`) walks through the phase end to end and has been executed against the seeded stack.
6. **Every module in the phase ships at least one AI feature** that passes the platform gates (§1.3). A module without a passing AI feature is not complete.
7. A **"what works / what is stubbed / known gaps"** summary is written (template in §2.9).

### 1.3 AI platform gates (apply to every AI feature, every phase)

| Gate | Verifiable by |
|---|---|
| Permission-scoped tools | Tool-permission test suite: the feature invoked as user A never reads or writes a record user A cannot access via the UI (asserted against the ORM's own access checks). |
| Human confirmation for any write or outbound action | Every write/outbound path creates an `ai_core.proposal`; a test asserts zero business-record mutations and zero mail-queue rows until `proposal.accept()` is called by a user. |
| Audit logging | `ai_core.run` row exists for each invocation with user, company, feature, prompt version, model, provider, input/output references, token counts, cost, outcome (accepted/edited/rejected/none). |
| Evaluation set with release threshold | `ai/evals/<feature>.jsonl` ≥ 30 cases (≥ 100 for scoring/forecasting), threshold declared in `ai/features.yaml`, `tessera ai eval run <feature>` passes in replay mode in CI. |
| Feature flag | `ai_core.feature_flag` row exists; disabled → UI affordance hidden, API returns 404 `feature_disabled`, no gateway call made. Kill switch per company works the same way. |
| Disclosure | Output carries `ai_generated=true` and renders the "AI-drafted" label until a human edits or accepts. |
| Guardrails | Prompt-injection corpus run: 0 unauthorised tool calls, 0 schema-invalid outputs accepted, PII redaction applied where feature does not need PII. |
| Compliance classification | `ai/features.yaml` declares `risk_class` (minimal / limited / high) and, for high-risk employment features, links bias-test results and human-decision documentation. |

### 1.4 Universal acceptance criteria (checked at every phase boundary)

| # | Criterion |
|---|---|
| U-1 | Fresh clone → `docker compose up` → login as demo admin → every menu of the phase's modules opens without console errors. |
| U-2 | `tessera module install <every module in phase>` on an empty DB completes; `tessera module uninstall` of each leaf module succeeds and `tessera db check` is clean afterwards. |
| U-3 | Upgrade path: a DB seeded at the previous phase, after `tessera db migrate`, passes the current test suite. |
| U-4 | Cross-company test: a user restricted to company A sees zero company-B records in every list, search, export, report, API and AI retrieval of the phase's models. |
| U-5 | Field-level access test: a field with `groups=` is absent from `read`, export, API response, OpenAPI schema and AI tool results for a user outside the group. |
| U-6 | Performance: standard list view of each primary model with 100k seeded rows returns < 300 ms p95 (nightly perf job). |
| U-7 | Accessibility: Playwright + axe reports zero serious/critical violations on every new view type and module home page. |
| U-8 | i18n: every user-facing string of the phase is translatable; the `fr_FR` catalog is ≥ 95% complete for new strings (machine-translated and flagged for review is acceptable). |
| U-9 | Security: OWASP ASVS L2 checklist delta for the phase reviewed; no high-severity finding open. |
| U-10 | Observability: every new job and AI feature emits metrics (count, latency, failure, and for AI tokens/cost) visible on the Prometheus endpoint. |
| U-11 | All AI features in the phase pass the gates in §1.3. |

---

## 2. Phase 1 — Foundation and CRM

Modules: `core`, `chatter`, `web`, `workflow`, `reports`, `studio`, `ai_core`, `portal`, `contacts`, `discuss`, `calendar`, `crm`, `product` (minimal), `sale` (quotations only — D-011).

Phase 1 is delivered in sub-phases 1.0–1.7 (D-021). Each sub-phase ends runnable and tested.

### 2.0 Sub-phase 1.0 — Repository and tooling

**Scope**
- Monorepo layout per ARCHITECTURE §3; `backend/pyproject.toml` (uv/pip-tools lock), `frontend/package.json` (pnpm), shared lint/format config (ruff, mypy strict on kernel, eslint, prettier, tsc strict).
- Docker Compose stack: Postgres 16 + pgvector, Redis 7, MinIO, Mailpit, `web`, `realtime`, `worker`, `scheduler`, `gateway`, `frontend` dev server, optional `ollama` profile.
- GitHub Actions: lint → unit/integration (Postgres/Redis services) → frontend unit → e2e (Playwright, `--trace off`) → coverage gate → image build. Nightly workflow stub for perf and live AI evals (D-017).
- `tessera` CLI skeleton (Typer): `db init|migrate|check|reset`, `module scaffold|install|upgrade|uninstall|list`, `seed demo`, `test`, `serve|worker|scheduler|gateway`.
- `README.md`, `CONTRIBUTING.md`, `LICENSE` (Apache-2.0, D-018), `tools/rename_brand.py` (D-001).

**Deliverables:** compose stack, CI pipeline, CLI skeleton, pre-commit config, `docs/developer/getting-started.md`.

**Acceptance criteria**

| # | Criterion |
|---|---|
| 1.0-a | `git clone && docker compose up` on a machine with Docker only reaches `/readyz` = 200 for web, gateway and realtime within 3 minutes. |
| 1.0-b | `tessera db init` creates the database, enables `pgvector`, and `tessera db check` reports "no drift". |
| 1.0-c | CI runs on every PR and fails on: ruff error, mypy error in `tessera/`, tsc error, any failing test, coverage < 80% (gate initially applied to kernel packages that exist). |
| 1.0-d | `tessera module scaffold demo_widgets` produces a module that installs, contributes one model and one form view, and passes its generated smoke test. |
| 1.0-e | `tools/rename_brand.py --dry-run` lists every occurrence of the brand token and exits 0. |

### 2.1 Sub-phase 1.1 — Kernel

**Scope**
- Module system: `manifest.toml` parsing and JSON Schema validation, dependency closure and topological ordering, install/upgrade/uninstall lifecycle, `core.module`, `core.module_record` ownership tags, `core.module_migration_log`, per-database registry with Redis invalidation.
- Metadata ORM: all field types (`Char`, `Text`, `Html`, `Integer`, `Float`, `Monetary`, `Boolean`, `Date`, `Datetime`, `Selection`, `Many2one`, `One2many`, `Many2many`, `Binary`, `Image`, `Json`), computed (stored and non-stored, with dependency graph and batched recompute), related, `company_dependent`, `translate` (JSONB, D-009), `tracking`, `groups`, `pii`; model extension via `_extend` with MRO composition; mixins; recordset API (`search`, `browse`, `create`, `write`, `unlink`, `read`, `read_group`, `name_search`, `copy`, `exists`); per-transaction cache and prefetch.
- Domain DSL (D-005) compiler to SQLAlchemy Core (joins, `EXISTS`, recursive CTEs for `child_of`/`parent_of`) and in-memory evaluator; variables `$uid`, `$company`, `$companies`, `$now`, `$today`, `$user.<field>`.
- Schema sync (additive) + versioned migrations with mandatory `down.py` (D-007); `tessera db check`.
- Security: groups with implied groups, model access rights, record rules (global AND / group OR, per operation, re-check on write/unlink), field access, multi-company global rule, `env.elevated(reason=)` restricted to kernel paths and audited; Argon2id passwords; `system` user.
- `core` module data: `core.company` (hierarchy, settings), `core.user`, `core.group`, `core.currency` + `core.currency_rate`, `core.country` / `core.country_state`, `core.language`, `core.sequence` (gapless and standard), `core.attachment` (object storage), `core.audit_log`, archive (`active`) and soft delete (`deleted_at`, trash, restore — D-019), `core.pii_access_log`.
- Testing toolkit: pytest fixtures (per-test transaction rollback), factories, hypothesis strategies for domains.

**Deliverables:** `tessera.orm`, `tessera.modules`, `tessera.security`, `core` module, kernel test suite, `docs/developer/kernel.md`, `docs/developer/writing-a-module.md`.

**Acceptance criteria**

| # | Criterion |
|---|---|
| 1.1-a | `tessera module install core` on an empty DB completes in < 30 s; `tessera db check` reports no drift; a second run is a no-op. |
| 1.1-b | A test module `kernel_test_b` extends `kernel_test_a.thing` with a new stored computed field and an overridden method; after install both modules' `super()` chain executes in dependency order and the new column exists. Uninstalling `kernel_test_b` restores the original behaviour and renames (not drops) the column. |
| 1.1-c | Property-based test: for 1,000 randomly generated domains over a 3-model graph, SQL compilation and in-memory evaluation return identical record sets. |
| 1.1-d | Record-rule test: user with rule `[["team_id", "in", "$user.team_ids"]]` on `kernel_test_a.thing`: `search` returns only matching rows; `write` on a non-matching id raises `AccessError` and no `core.audit_log` row is written. |
| 1.1-e | Cross-company test (U-4) passes for `core.*` models with two seeded companies. |
| 1.1-f | Field-access test (U-5) passes for a `groups=`-protected field. |
| 1.1-g | A `translate=True` field written in `fr_FR` and read with `lang=de_DE` returns the company-language value, then `en_US`, per the fallback chain. |
| 1.1-h | Stored computed field across a many2one path (`partner_id.name`) recomputes on the dependent record when the related record is renamed; a write of 10,000 records triggers exactly one batched recompute statement per dependent field. |
| 1.1-i | Gapless sequence: 50 concurrent allocations produce 50 consecutive numbers with no gaps and no duplicates. |
| 1.1-j | Soft delete: a deleted record is absent from `search`, present in trash, restorable, and hard-purged by the scheduled purge after the retention window (clock-mocked). |
| 1.1-k | Migration test: `migrations/1.1.0/pre.py` + `post.py` + `down.py` run forward and back on a seeded DB and the test suite passes in both states. |
| 1.1-l | Kernel coverage ≥ 85%; mypy strict passes on `tessera/orm`, `tessera/security`, `tessera/modules`. |

### 2.2 Sub-phase 1.2 — API and client foundation

**Scope**
- Auth: session cookies + CSRF, API keys (hashed, scoped, expiring), TOTP 2FA with recovery codes, OIDC SSO (D-013), password policy and lockout, rate limiting (IP/user/key).
- Auto REST (`/api/v1/<model>…`), JSON-RPC (`/jsonrpc`), per-database OpenAPI, pagination (offset and keyset), filtering by domain, `expand`, ETags, whitelisted method calls; webhooks (`core.webhook`) with HMAC signatures, retries and delivery log.
- `web` module: menus (group-filtered), window/server/URL/report/client actions, client-extension registry, recent items, favourites.
- Views: JSON view schema and validation for all ten view types (D-010), view inheritance operations, `search` view model (filters, group-bys, favourites).
- Frontend shell: login (password, TOTP, OIDC), company switcher (multi-select), menu/action router, generic **form**, **list** (inline editing, grouping, aggregates, configurable columns, bulk actions), **kanban** (drag-drop, quick create), **search** panel; field widgets for all field types; domain/filter editor; keyboard shortcuts; dark mode; responsive layout; PWA shell (installable, offline splash); typed API client generated from OpenAPI; TanStack Query; realtime hub client.
- Realtime hub: WebSocket endpoint, Redis bus, channel subscriptions.

**Deliverables:** `tessera.api`, `tessera.security.auth`, `tessera.views`, `tessera.realtime`, `web` module, `frontend/src/kernel`, API contract tests (schemathesis), Playwright smoke suite, `docs/api/README.md`.

**Acceptance criteria**

| # | Criterion |
|---|---|
| 1.2-a | Login with password + TOTP works; 5 failed attempts lock the account for the configured window; CSRF-less `POST` from a foreign origin is rejected with 403. |
| 1.2-b | OIDC login against the Compose-provided Keycloak/dex test realm creates or links a user and assigns groups from claims per configuration. |
| 1.2-c | `GET /api/v1/core.user?domain=…&fields=…&limit=50` returns in < 200 ms p95 on the seeded DB, honours record rules and field access, and the response validates against the generated OpenAPI schema (schemathesis run: zero schema violations across all exposed models). |
| 1.2-d | A user without the `core.group_admin` group calling `PATCH /api/v1/core.user/{other_id}` receives 403 and no audit row is written. |
| 1.2-e | Webhook on `core.company` write delivers within 5 s with a valid HMAC signature; a failing endpoint is retried with exponential backoff and the delivery log shows each attempt. |
| 1.2-f | A view JSON file with an unknown node kind fails module install with a schema error naming the file and JSON pointer. |
| 1.2-g | View inheritance: a module applying `insert_after`, `replace`, `remove`, `set_attr` against another module's form produces the expected composed view (golden snapshot). |
| 1.2-h | Form, list and kanban render any model with no module-specific client code (verified against the scaffolded module from 1.0-d). |
| 1.2-i | List inline edit of a `Monetary` field respects currency rounding and optimistic concurrency (second writer gets a conflict dialog). |
| 1.2-j | Keyboard: `Ctrl/⌘-K` opens the command palette; `Ctrl/⌘-S` saves a form; focus order on the form view passes the axe keyboard checks. |
| 1.2-k | PWA: Lighthouse PWA audit passes installability; offline reload shows the app shell with a clear offline notice. |
| 1.2-l | Realtime: a write on a record a second browser has open updates that browser's form within 1 s. |

### 2.3 Sub-phase 1.3 — Platform services

**Scope**
- `chatter` module: `chatter.thread` mixin, messages (comment/note/notification/email/system), followers and subtypes, tracking values, reactions, attachments on threads, `chatter.activity` with types and the per-user activity dashboard (activity view), `@mentions`.
- Notifications (`core.notification`): in-app via realtime, email digests, Web Push registration (push-ready).
- Jobs (D-008): Celery app, `core.job` ledger, retry policies, dead-letter, idempotency keys, job UI; scheduler process over `core.scheduled_job` with Redis lock; `core.mail_queue` with per-company SMTP, bounce handling; inbound IMAP fetcher and alias routing (`message_new`/`message_update`); Mailpit in dev.
- `workflow` module: state-machine definitions as data, transitions with guards and groups, status-bar rendering; automated actions (`on_create`, `on_write`, `on_delete`, `on_time`, `on_transition`, `on_webhook`) with the sandboxed expression language.
- Import/export (`core.import_job`): CSV/XLSX, mapping suggestions, saved mappings, dry run with per-row/column errors, background execution with progress; export with related paths and templates.
- Global search (`web.search_index`, trigram), command palette over records/menus/actions.
- `reports` module: WeasyPrint engine in worker, `reports.template` versioning, `reports.layout` per company (header/footer/logo/paper), QR codes, basic block-based template editor.
- Remaining Phase 1 renderers: **calendar**, **pivot**, **graph**, **activity** views.

**Deliverables:** `chatter`, `workflow`, `reports` modules; `tessera.jobs`, `tessera.reports`; job and mail-queue admin UIs; `docs/admin/email.md`, `docs/admin/jobs.md`, `docs/dev/workflows.md`, `docs/dev/platform-services.md`.

**Acceptance criteria**

| # | Criterion |
|---|---|
| 1.3-a | Posting a message on a thread notifies followers in-app within 1 s and queues one email per follower whose preference is email; the mail appears in Mailpit with the thread reply-to alias. |
| 1.3-b | Replying to that email via IMAP (Mailpit → fetcher) appends a `chatter.message` of type `email` to the same thread with the attachment preserved. |
| 1.3-c | Changing a `tracking=True` field writes one `chatter.tracking_value` with old and new display values, visible in the thread. |
| 1.3-d | Activity view: overdue / today / planned buckets are correct across timezones (test users in `UTC`, `America/Los_Angeles`, `Asia/Kolkata`). |
| 1.3-e | A job that raises is retried per its policy with backoff, lands in dead-letter after max attempts, and can be re-queued from the UI; the ledger shows every attempt. |
| 1.3-f | Scheduler: with three scheduler replicas running, a `*/1 * * * *` job runs exactly once per minute (lock test). |
| 1.3-g | A `workflow.transition` with a guard domain that fails is not shown in the status bar and returns 409 when called via API. |
| 1.3-h | Automated action `on_time` (3 days before a date field) fires once per record (clock-mocked) and never twice. |
| 1.3-i | Import dry run of a 10,000-row CSV with 25 deliberate errors reports exactly those 25 by row and column and writes nothing; the real run creates 9,975 records in a background job with progress events. |
| 1.3-j | Export respects field-level access: a protected field is absent from the file for a non-group user even if selected. |
| 1.3-k | PDF report with per-company header/footer and QR code renders in < 2 s in the worker; the QR decodes to the configured URL. |
| 1.3-l | Pivot `read_group` on 100k rows with two row groups and one column group returns < 500 ms p95; graph view renders the same aggregates. |

### 2.4 Sub-phase 1.4 — Applications

**Scope**

*Contacts (`contacts`)*: companies and individuals, parent/child hierarchy, contact types (invoice/delivery/other), tags, addresses, duplicate detection (rule-based: email/phone/name+company) and merge wizard with relation re-pointing, portal access grant.

*Discuss (`discuss`)*: channels (public/private), direct messages, mentions, threads, file sharing, emoji reactions, presence, unread counters, notification settings.

*Calendar (`calendar`)*: day/week/month views, shared calendars and visibility, recurring events, attendees and RSVP, rooms (`calendar.room`) with booking and conflict detection, meeting scheduling links, external sync adapters: CalDAV (two-way), Google Calendar (OAuth, two-way), Microsoft 365 via Graph (OAuth, two-way) — the adapter interface ships with CalDAV first, Google and Microsoft behind the same interface.

*CRM (`crm`)*: leads and opportunities with lead→opportunity conversion (merge into existing partner/opportunity); intake from email alias, web form (embeddable), API and manual entry; pipelines with configurable stages, probability, expected revenue, close dates, won/lost reasons; kanban, list, calendar, pivot, graph and forecast views; sales teams with round-robin and rule-based auto-assignment; rule-based lead scoring (`crm.scoring_rule`); activity management and per-user activity dashboard; quotation creation from an opportunity; win-rate, sales-velocity and funnel reports; customer portal for quotes and documents.

*Product (`product`, minimal — D-011)*: products, categories, sale price, cost, unit, tax placeholder, images, active flag.

*Sale (`sale`, quotations only — D-011)*: quotation with lines, totals, validity, template text, PDF, send by email, portal view with accept/reject and signature-by-name placeholder, states `draft → sent → accepted | rejected | expired`; accepting marks the linked opportunity won.

*Portal (`portal`)*: portal users and groups, narrow record rules, document list, quote view, profile.

**Deliverables:** modules above with views, security, seed and demo data, reports (quotation PDF), `docs/user/<module>.md` quick guides, Playwright scenarios per module.

**Acceptance criteria**

| # | Criterion |
|---|---|
| 1.4-a | `tessera module install crm` on an empty DB (pulling `contacts`, `chatter`, `calendar`, `product`, `sale`, `ai_core`) completes in < 60 s and `tessera db check` reports no drift. |
| 1.4-b | Contacts merge: merging three duplicates re-points every `Many2one` to the survivor across all installed models, preserves chatter history, and is undoable within the undo window. |
| 1.4-c | Discuss: a message in a channel with 50 members reaches all connected clients within 1 s; an `@mention` creates a notification; an uploaded file is downloadable only by channel members. |
| 1.4-d | Calendar: CalDAV two-way sync against the Compose Radicale server round-trips create/update/delete of a recurring event within one sync cycle; Google and Microsoft adapters pass the same contract test suite against recorded fixtures. |
| 1.4-e | Room booking: two overlapping bookings of the same room are rejected with a conflict error that names the clashing event. |
| 1.4-f | An email to `crm@<domain>` creates a lead with sender as contact, body as description and attachments attached; the embeddable web form creates a lead and fires the `on_create` automation. |
| 1.4-g | Round-robin assignment distributes 100 new leads across 4 team members 25 ± 1 each; rule-based assignment routes by country rule first. |
| 1.4-h | Rule-based lead scoring: changing a scoring rule recomputes scores for the affected open leads in a background job; score contributions are visible per lead. |
| 1.4-i | Lead → opportunity conversion with "merge into existing" moves chatter, activities and attachments and archives the source. |
| 1.4-j | A user without `crm.group_manager` calling `PATCH /api/v1/crm.lead/{id}` on another team's lead receives 403 and no audit row is written; `crm.group_manager` succeeds and the audit row records before/after. |
| 1.4-k | List view of 100k `crm.lead` rows (seeded) returns < 300 ms p95; kanban grouped by stage with counts and expected-revenue sums returns < 500 ms p95. |
| 1.4-l | Forecast view (grouped by expected close month with probability-weighted revenue) matches a SQL cross-check to the cent. |
| 1.4-m | Quotation from opportunity: PDF renders with company layout; "Send" queues an email with a tokenised portal link; portal acceptance sets quotation `accepted` and the opportunity `won`, each with audit rows. |
| 1.4-n | Portal user sees only their own partner's quotes (U-4/U-5 variant) and cannot call any non-portal method (403). |
| 1.4-o | Win-rate, sales-velocity and funnel reports reconcile to `read_group` cross-checks on the demo seed. |

### 2.5 Sub-phase 1.5 — AI platform

**Scope**

*Gateway service (`gateway/`)*
- Provider adapters: Anthropic (Messages API, strict tools, structured outputs, streaming, prompt caching, `inference_geo`), OpenAI, Azure OpenAI, Google, OpenAI-compatible self-hosted (vLLM / Ollama), `mock` (deterministic, for tests); embeddings adapters (OpenAI, Voyage, self-hosted — D-015).
- Per-company provider configuration and region; task-class routing table (`classify`, `extract`, `embed`, `summarize`, `draft`, `reason`, `translate`, `vision`, `transcribe`) with tiers (D-016); fallback to a second provider on outage with circuit breakers.
- Response cache (prompt version + redacted input hash), budgets and quotas (per company spend/tokens, per user), PII redaction before external calls (configurable per feature), structured-output schema validation with one bounded repair retry, unsafe-output filter, kill switches (per feature, per company, global), retention settings per company, usage/cost ledger, admin cost dashboard data endpoints, streaming pass-through (SSE).

*AI runtime (`tessera.ai` + `ai_core` module)*
- Prompt registry (`ai_core.prompt`, `ai_core.prompt_version`): parameterised YAML prompts loaded from modules, versioned, tested, rolled out behind flags; no prompts in business code.
- Tools framework: declarative tool definitions bound to ORM methods, executed with the requesting user's `Environment`; read tools vs proposal-producing tools; tool-permission tests.
- Retrieval: `ai_core.embedding` (pgvector, HNSW), chunking and indexing jobs over records, chatter, attachments (text-extracted) and knowledge-type models; retrieval filters by record rules **before** ranking (SQL join on accessible-id subquery); citations with record links.
- Proposals (`ai_core.proposal`): diff preview, accept / edit / reject, apply with the user's permissions, audit linkage.
- Feedback (`ai_core.feedback`): thumbs up/down, corrections → evaluation dataset export.
- Evaluation harness: `tessera ai eval run|report`, golden sets per feature, thresholds in `ai/features.yaml`, replay cassettes (D-017), CI gate, accuracy/latency/cost report per feature.
- Feature flags and kill switches (`ai_core.feature_flag`), compliance registry (risk class, bias-test links, human-decision requirement), disclosure labelling, explainability payload schema (top contributing factors).
- Observability: metrics and traces per feature; graceful degradation when gateway/provider is down.

**Deliverables:** gateway service and image, `ai_core` module, `tessera.ai`, eval CLI, prompt-injection corpus, cost dashboard (admin), `docs/ai/governance.md` (data use, model choices, risk classification, limitations), `docs/ai/developer-guide.md`.

**Acceptance criteria**

| # | Criterion |
|---|---|
| 1.5-a | Each adapter passes the shared adapter contract test (chat, streaming, tool call, structured output, embeddings where supported) against recorded fixtures; the `mock` adapter passes it live. |
| 1.5-b | Routing: with company A routed to Anthropic and company B to a self-hosted Ollama endpoint, the same feature invoked by each company hits the configured provider (asserted on the gateway ledger) and never the other. |
| 1.5-c | Fallback: primary provider returning 5xx/timeouts for 3 consecutive calls opens the circuit; the next call succeeds on the secondary; the ledger records `fallback=true`. |
| 1.5-d | Budget: a company at 100% of monthly spend receives `budget_exceeded`; the UI shows the manual path; no provider call is made. A user over quota is blocked while others in the company proceed. |
| 1.5-e | Redaction: for a feature flagged `pii=redact`, emails, phone numbers and names in the input are replaced with placeholders in the provider request body (captured by the mock adapter) and restored in the output. |
| 1.5-f | Schema validation: an output failing its JSON Schema is retried once with the error; a second failure returns `invalid_output` and nothing is written. |
| 1.5-g | Tool permission: across the 50-record prompt-injection corpus (records and emails containing instructions to read other companies' data, change records or send mail), there are **0** tool calls outside the user's permissions and **0** records mutated without a proposal. |
| 1.5-h | Retrieval: a query by a user restricted to company A never returns a chunk from company B, even when B's chunk is the nearest vector (adversarial fixture); every answer chunk carries a citation resolving to a record the user can open. |
| 1.5-i | Proposal lifecycle: accept writes exactly the proposed diff with the user's permissions; edit stores the edited version and marks `edited`; reject writes nothing; all three outcomes are visible in the `ai_core.run` audit. |
| 1.5-j | Kill switch: disabling a feature for one company hides its UI, returns 404 `feature_disabled` on its API and produces zero gateway calls; other companies are unaffected. |
| 1.5-k | `tessera ai eval run` in replay mode is deterministic across 3 runs and fails CI when a threshold in `ai/features.yaml` is lowered below the recorded score. |
| 1.5-l | Streaming: the first token of a drafted reply reaches the browser < 1.5 s after request on the mock adapter; the UI shows the "AI-drafted" label until the user edits or accepts. |
| 1.5-m | Gateway down: AI affordances show "unavailable", the manual workflow completes, and no request errors surface outside the AI panel. |
| 1.5-n | Cost dashboard shows per-company, per-feature tokens, spend, latency p50/p95, acceptance rate, matching the ledger to the cent for the seeded runs. |

### 2.6 Sub-phase 1.6 — Phase 1 AI features

Every feature below ships with a prompt version, tools, flag, eval set and guardrail tests. "Interactive" = ≤ 3 s p95; otherwise background with notification.

| Module | Feature | Task class / tier | Inputs | Output | Write or outbound? | Eval metric → threshold | Latency |
|---|---|---|---|---|---|---|---|
| discuss | Thread / channel summary | summarize / mid | messages in range (rule-filtered) | text + cited message ids | no | LLM-judge faithfulness ≥ 4.2/5; 0 hallucinated citations | background (> 50 msgs) / interactive |
| discuss | "Catch me up" | summarize / mid | unread messages across channels | ranked digest with links | no | coverage of key items ≥ 85% on golden set | interactive |
| discuss | Reply draft | draft / mid | thread context, user tone profile | draft message | **outbound → proposal** | judge usefulness ≥ 4/5; tone match ≥ 80% | interactive (stream) |
| discuss | Translation | translate / small | message, target lang | translated text labelled | no | COMET/chrF vs reference ≥ threshold per language pair | interactive |
| calendar | Meeting brief | summarize / mid | event, attendees, linked records (rule-filtered) | brief with citations | no | judge completeness ≥ 4/5; 0 unauthorised facts | background on event −1h / on demand |
| calendar | Suggested meeting times | reason / small | attendee availability, constraints | ranked slots | no (booking is user action) | top-3 contains valid slot ≥ 95% | interactive |
| calendar | Natural-language scheduling | extract / small | free text | structured event draft | **write → proposal** | field extraction F1 ≥ 0.9 | interactive |
| contacts | Duplicate detection (fuzzy + semantic) | embed+classify / small | partner record, candidate set | ranked duplicate candidates with reasons | merge → proposal | precision ≥ 0.9, recall ≥ 0.8 on labelled set | background on create / on demand |
| contacts | Tag & hierarchy suggestions | classify / small | partner fields, similar partners | suggested tags / parent | **write → proposal** | top-3 accuracy ≥ 0.8 | interactive |
| contacts | Enrichment with source attribution | extract / small | partner, enrichment adapter results | field suggestions + sources | **write → proposal** | field accuracy ≥ 0.9 on fixture set; 100% of suggestions carry a source | background |
| crm | Predictive lead scoring & win probability with explanations | classify / small (+ tabular model) | lead features, history | score, probability, top-5 factors | no (score field written by system job, labelled AI) | AUC ≥ 0.75 on back-test; explanation factor validity ≥ 90% | background nightly + on change |
| crm | Next-best-action | reason / mid | lead, activities, stage history | ranked actions with rationale | schedule activity → proposal | judge relevance ≥ 4/5 | interactive |
| crm | Deal risk alerts | classify / small | stale activity, stage age, sentiment | risk flag + reasons | notification only | precision ≥ 0.7 on back-test | background daily |
| crm | Lead routing | classify / small | lead, team rules, load | suggested assignee | **write → proposal** (auto-apply only if rule allows) | accuracy vs manager choice ≥ 0.8 | interactive on create |
| crm | Lead extraction from email / forms | extract / small | raw email or form payload | lead fields (schema) | **write → proposal** (auto-create lead as draft is allowed; field fill is proposal) | F1 ≥ 0.9 per field class | background |
| crm | Forecasting with confidence ranges | reason / mid (+ stats) | pipeline, history | P10/P50/P90 per period with explanation | no | MAPE ≤ 20% on back-test; coverage of P10–P90 ≥ 80% | background |
| crm | Follow-up & proposal drafts | draft / mid | lead, chatter, products | email/proposal draft | **outbound → proposal** | judge ≥ 4/5; 0 fabricated prices | interactive (stream) |
| crm | Call & meeting summaries with field suggestions | transcribe+extract / mid | transcript/notes | summary + proposed field values | **write → proposal** | summary faithfulness ≥ 4.2/5; field F1 ≥ 0.85 | background |
| crm | Objection handling from past deals | reason+retrieve / mid | objection text, won/lost deals (rule-filtered) | suggested responses with cited deals | no | judge helpfulness ≥ 4/5; 100% citations resolvable | interactive |
| crm | Natural-language search → filters | extract / small | query text, model metadata | domain DSL for review | no (user applies) | exact-match or equivalent domain ≥ 0.85 | interactive |
| chatter / activities | AI-ranked daily to-do | reason / small | user's activities, deadlines, deal values | ranked list with reasons | no | rank correlation with manager labels ≥ 0.6 | background morning + on demand |

**Acceptance criteria**

| # | Criterion |
|---|---|
| 1.6-a | Every row above has: `ai/features.yaml` entry (flag, risk class, tier, threshold), prompt version, eval set meeting the minimum size, replay cassettes, guardrail tests — enforced by `tessera ai eval report --strict` returning 0 missing items. |
| 1.6-b | All thresholds in the table pass in replay CI; the nightly live run produces a report with accuracy, latency p95 and cost per feature. |
| 1.6-c | All proposal-producing features: zero mutations and zero outbound mail without `proposal.accept()` (automated test per feature). |
| 1.6-d | Lead scoring UI shows the top contributing factors and a "why" panel; no protected attribute (gender, age, ethnicity, religion, disability, nationality of a *person*) is a model input — verified by a feature-list assertion test. |
| 1.6-e | NL search: "open deals over 10k in EMEA closing this quarter owned by me" produces a reviewable domain that the user applies with one click; incorrect fields are editable in the filter editor. |
| 1.6-f | Interactive features meet ≤ 3 s p95 on the mock adapter perf run; background features post a notification with a link on completion. |
| 1.6-g | Every AI-generated text in Phase 1 renders with the "AI-drafted" label and loses it on human edit/accept (e2e). |

### 2.7 Sub-phase 1.7 — Studio v1 (D-012)

**Scope**
- Add fields of any type to any model (runtime additive schema sync in a transaction), with widgets, defaults, required/readonly conditions, groups.
- Edit form/list/kanban layouts via drag-and-drop; create new views; edit search filters; produce view-inheritance operations stored as `studio.view_change`.
- Define automated actions and simple state machines without code (via `workflow`).
- Versioning: every Studio change is a `studio.change` with before/after metadata; rollback per change or to a snapshot; export/import of customisations as a module (`studio_customizations_<company>`).
- Permissions: `studio.group_designer`; changes scoped per company where applicable.

**Acceptance criteria**

| # | Criterion |
|---|---|
| 1.7-a | Adding a `Monetary` field to `crm.lead` via Studio creates the column, field metadata, list/form placement and API exposure without restart; `tessera db check` is clean. |
| 1.7-b | Rolling back the change removes the field from views and metadata and renames the column per D-007; a second rollback is a no-op. |
| 1.7-c | A Studio-created automated action (on stage change → schedule activity) fires in the e2e test. |
| 1.7-d | Exported customisations install as a module on a fresh DB and reproduce the same views and fields (golden snapshot). |
| 1.7-e | A non-designer user receives 403 on every Studio endpoint. |

### 2.8 Phase 1 demo script (outline)

1. `docker compose up` → `tessera seed demo` → login as admin; show company switcher (two companies, two currencies, `en_US`/`fr_FR` users).
2. Contacts: create a company and two contacts; show duplicate detection proposing a merge; accept the merge; show undo.
3. Discuss: post in `#sales`, `@mention` a colleague, show realtime delivery and in-app notification; ask "Catch me up".
4. Calendar: book a room; show conflict rejection; type "lunch with Dana next Tuesday 12:30" → AI proposes an event → accept.
5. CRM intake: send an email to the `crm@` alias (Mailpit) → lead appears with extracted fields proposed → accept; submit the web form → second lead.
6. Lead routing proposal → accept; show round-robin on team.
7. Kanban: drag lead to next stage; show tracking in chatter and probability update.
8. Convert lead to opportunity, merging into an existing contact.
9. Open the opportunity: lead score with top factors; next-best-action; deal-risk badge with reasons.
10. Draft a follow-up email with AI → edit → send (shows "AI-drafted" label lifecycle).
11. Paste call notes → summary + proposed field changes → accept some, reject others; show audit trail of the run.
12. Natural-language search: "my open deals over 10k closing this quarter" → review generated filters → apply → save as favourite.
13. Create a quotation from the opportunity; render PDF with company layout and QR; send; open portal link as customer; accept → opportunity marked won.
14. Reports: win-rate, sales velocity, funnel; pivot and graph views; forecast with confidence range.
15. Studio: add a "Competitor" field to leads, place it on the form, add an automation; roll it back.
16. Admin: AI cost dashboard, feature flags, kill switch demonstration (disable "Reply draft" for company B), import 10k leads via CSV with dry run, job ledger.

### 2.9 "What works / stubbed / known gaps" template

```
## Phase N summary
### Works end to end
- <module>: <capability> (tests: <path>)
### Stubbed or partial
- <capability>: <what is stubbed>, <why>, <tracked in DECISIONS/issue>
### Known gaps
- <gap>: <impact>, <planned phase>
### AI features
| feature | flag | eval score vs threshold | live run date | acceptance rate (demo) |
```

---

## 3. Phase 2 — Sales and Accounting

Modules: `sale` (full), `product` (full), `sale_subscription`, `sale_rental`, `sale_commission`, `account`, `account_bank`, `account_tax`, `account_analytic`, `account_budget`, `account_asset`, `account_reports`, `account_dunning`, `account_einvoice`, `payment`.

**Scope**
- *Sales*: product variants and attributes; pricelists by customer, region, quantity and date; discounts and promotions; quotation templates, optional products and upsells; sales orders; delivery and invoicing policies (ordered/delivered quantities, milestones, down payments); commission plans and statements; customer returns (credit flow; stock integration arrives in Phase 3); subscriptions with recurring billing, proration, renewals, churn tracking; rental orders with availability calendars and late-return fees.
- *Invoicing and accounting*: chart of accounts from country templates (ship ≥ 3: generic, US, EU-style); journals; fiscal years and periods with close and lock dates; customer invoices, credit notes, vendor bills, refunds with draft/post/cancel; payments (partial, batch), payment terms, early-payment discounts; bank statement import (CSV, OFX, CAMT.053, MT940) with reconciliation rules and auto-matching; taxes (rates, groups, inclusive/exclusive, fiscal positions, reverse charge, tax return report); multi-currency with realised and unrealised FX gain/loss; analytic accounting (cost centres, projects, departments) and budgets with budget-vs-actual; fixed assets with depreciation schedules; reports (general ledger, trial balance, P&L, balance sheet, cash flow, aged receivables/payables, partner ledger, consolidated multi-company); dunning and payment reminders; e-invoicing (Peppol/UBL) via country adapters (ship UBL 2.1 generation + one Peppol access-point adapter interface with a mock).
- *Payments*: payment-provider adapter interface (Stripe adapter + mock) for portal invoice payment; SAML SSO (D-013).

**AI features**

| Module | Feature | Tier | Write/outbound? | Eval metric → threshold |
|---|---|---|---|---|
| sale | Upsell / cross-sell suggestions | small | add line → proposal | top-3 precision ≥ 0.6 on history back-test |
| sale | Discount suggestions within approved bands | small | write → proposal; never outside band (hard check) | 100% within band; acceptance ≥ 50% on demo |
| sale | Quote lines from pasted text / RFP | mid | write → proposal | line F1 ≥ 0.85; 0 fabricated products (must map to catalogue or be flagged) |
| sale | Pricing-policy breach flags | small | notification | precision ≥ 0.9 |
| sale_subscription | Churn prediction | small + tabular | no (score) | AUC ≥ 0.75; explanations present |
| sale_subscription | Personalised renewal outreach drafts | mid | outbound → proposal | judge ≥ 4/5 |
| sale_rental | Rental demand forecasting | mid + stats | no | MAPE ≤ 25% |
| sale_rental | Usage anomaly alerts | small | notification | precision ≥ 0.7 |
| sale_commission | Plain-language commission statement explanation | mid | no | 100% numeric consistency with statement; judge clarity ≥ 4/5 |
| account | Invoice/bill OCR with auto-coding to accounts and taxes | extract (vision) / mid | write → proposal | header fields F1 ≥ 0.9; line F1 ≥ 0.8; account/tax suggestion accuracy ≥ 0.85 |
| account_bank | Reconciliation suggestions | small | reconcile → proposal | top-1 match accuracy ≥ 0.85 |
| account | Anomaly detection (duplicate bills, unusual amounts, unexpected vendors) | small | notification/flag | duplicate recall ≥ 0.95; false-positive rate ≤ 5% |
| account | Cash-flow forecasting | mid + stats | no | MAPE ≤ 20% on back-test |
| account_reports | Natural-language reports | mid | no (generates report parameters for review) | parameter accuracy ≥ 0.85; numbers always from report engine, never model |
| account_dunning | Personalised dunning messages | mid | outbound → proposal | judge tone/compliance ≥ 4/5; 100% correct amounts |
| account_budget | Plain-language variance explanations | mid | no | numeric consistency 100%; judge ≥ 4/5 |
| account_asset | Asset classification suggestions; depreciation anomaly checks | small | write → proposal / flag | classification accuracy ≥ 0.85; anomaly precision ≥ 0.8 |
| account_tax | Pre-filing anomaly checks with explanations | small | flag | recall ≥ 0.9 on seeded anomalies |

**Acceptance criteria**

| # | Criterion |
|---|---|
| 2-a | After `tessera seed demo` the trial balance sums to zero per company and in the consolidated view; P&L + balance sheet reconcile (net income → equity). |
| 2-b | Posting an invoice allocates a per-journal, per-fiscal-year gapless number (`INV/2026/00001`); cancelling a posted invoice is refused once the period is locked. |
| 2-c | A USD invoice paid in EUR at a later rate produces the realised FX gain/loss entry; the month-end unrealised FX job produces reversible entries. |
| 2-d | Bank import: the sample CAMT.053, MT940, OFX and CSV files each import without loss; the CAMT.053 demo statement reconciles ≥ 90% automatically with the shipped rules; the remainder are proposed by AI. |
| 2-e | Tax engine: inclusive and exclusive prices, fiscal-position mapping and reverse charge produce the expected entries on a 40-case golden set; the tax return report ties to the ledger. |
| 2-f | Subscription: proration on mid-cycle upgrade, renewal invoice generation and churn flag behave per the 20-scenario fixture; rental availability calendar prevents double-booking. |
| 2-g | Fixed asset: straight-line and declining schedules match the golden table; disposal posts gain/loss. |
| 2-h | UBL 2.1 output validates against the official schematron; the Peppol mock adapter round-trips send/receive status. |
| 2-i | OCR eval set (≥ 100 bills, multi-language, scans and digital) meets the thresholds above; every auto-coded line is a proposal. |
| 2-j | Portal payment via the Stripe test adapter creates a payment and reconciles the invoice; webhook replay is idempotent. |
| 2-k | Commission statement for the demo plan equals the independent spreadsheet cross-check; AI explanation references only numbers present in the statement. |
| 2-l | Phase 1 DB upgraded to Phase 2 passes the full suite (U-3); `account` reports on 100k lines render < 2 s p95. |

### 3.1 Sub-phases (D-069)

Data model: [ERD_PHASE_2.md](ERD_PHASE_2.md).

| Sub-phase | Modules | Exit tests |
|---|---|---|
| 2.0 Plan and data model | — | ERD, decisions D-069–D-074 |
| 2.1 Accounting core | `account` (charts: generic, US, EU), `product` 2.0 units | 2-b, 2-c, tax-engine part of 2-e, ledger invariants (D-073), portal invoices |
| 2.2 Product and sales | `product` 2.0, `sale` 2.0 | variants, pricelists, promotions, templates, invoicing policies, down payments, returns; Phase 1 → 2 migration (U-3) |
| 2.3 Bank, payments, dunning, e-invoicing | `account_bank`, `payment`, `account_dunning`, `account_einvoice` | 2-d, 2-h, 2-j |
| 2.4 Management accounting and reports | `account_analytic`, `account_budget`, `account_asset`, `account_reports`, `account_tax` | 2-a, 2-e (tax report), 2-g, report performance in 2-l |
| 2.5 Recurring revenue and commissions | `sale_subscription`, `sale_rental`, `sale_commission` | 2-f, 2-k |
| 2.6 AI features | every Phase 2 module | §1.3 gates, 2-i |
| 2.7 Closing | `auth_saml`, demo data, docs | U-1–U-11, 2-l, demo script, summary |

---

## 4. Phase 2b — E-Signature (Sign)

Modules: `sign`, `sms` (transport only — provider adapters, outbound queue, delivery status, opt-out registry — pulled forward for OTP codes and SMS signer links, D-022), bridges `sign_crm`, `sign_sale`, `sign_purchase` (Phase 3), `sign_hr` (Phase 5), `sign_project` (Phase 4), `sign_documents`, `sign_knowledge`, `sign_approvals` (Phase 8 — bridges land with their target modules).

**Deliverables by spec section**

| Spec | Deliverable |
|---|---|
| A. Preparation | PDF/DOCX/image intake with normalisation to PDF (original kept, both hashed SHA-256); PDF form-field detection and conversion; drag-and-drop field editor (resize, snap) with all field types (signature, initials, text, multi-line, date, checkbox, radio, dropdown, number, email, phone, full name, company, attachment, static stamp/image); per-field role, required flag, validation rules, defaults, conditional visibility; roles per document (name, type, order, auth level); merge fields from linked records with preview; versioned templates (edit → new version; sent envelopes keep theirs); bulk send from CSV or CRM list with per-recipient merge data. |
| B. Routing | Sequential, parallel and mixed signing groups; CC recipients; expiry, reminder schedule, per-recipient message; delivery via email link, SMS link, portal login, in-person/kiosk; void/cancel with reason, reassign (logged), add signer mid-flow (sender re-approval), resend; decline with reason; delegation recorded. |
| C. Signer experience | Mobile-first, WCAG 2.1 AA, signer language; consent screen; draw/type/upload signature with saved signature consent; guided next-field; completion blocked until required fields valid; final review; tokenised expiring links bound to verified identity after auth. |
| D. Identity | Levels 0–4: email link; OTP by email/SMS; portal/SSO login; government ID + selfie via provider adapter (mock + one real adapter interface); QES via TSP adapter interface (mock). Level per role, overridable per template. |
| E. Integrity & evidence | SHA-256 at creation, each signature, completion; PAdES signature with platform X.509 (option: TSP certificate); RFC 3161 timestamp; Certificate of Completion page with every event (timestamp, IP, UA, verified email/phone, hashes); hash-chained audit log; QR to verification page; **standalone verification tool** (`tools/sign-verify`, no app dependency); evidence store per envelope. |
| F. Lifecycle | States draft, sent, viewed, partially signed, completed, declined, expired, voided, failed; every transition logged and notified; completed envelopes immutable; retention per company/template; legal hold; GDPR erasure keeps hashes and event log. |
| G. Storage & security | Encryption at rest; signer tokens random, stored hashed, rate limited; record-rule visibility; template/void permissions; per-company residency; logs never contain document content. |
| H. Integrations | CRM (quote signed → opportunity won, order confirmed), Sales/Subscriptions, Documents; later bridges per phase; API + webhooks for create/add role/send/status/download/void with lifecycle events. |
| I. Reporting | Turnaround, completion, decline, expiry by template/sender/period; pending dashboard by signer and overdue; exportable audit report per envelope. |
| J. Testing | See acceptance criteria. |
| K. Deliverables | Seeded demo templates; API reference; signer help text; admin guide; legal-evidence note (ESIGN/UETA, eIDAS, Electronic Transactions Act 1999 AU) with the "supports evidence, not legal advice" disclaimer. |

**AI features**

| Feature | Tier | Write/outbound? | Eval metric → threshold |
|---|---|---|---|
| Merge-field pre-fill from existing contracts | extract / mid | write → proposal (preview before send) | field F1 ≥ 0.9 on contract fixture set |
| Summary of changes between template/document versions | summarize / mid | no | judge completeness ≥ 4/5; 0 invented changes vs diff ground truth |
| Risky-clause flags (advisory only) | classify / mid | no; labelled advisory, never blocks | recall ≥ 0.8 on labelled clause set; UI disclaimer present 100% |

**Acceptance criteria**

| # | Criterion |
|---|---|
| 2b-a | Unit/integration: sequential order enforces that signer 2 cannot open before signer 1 completes; parallel completes in any order; mixed groups honour group order. |
| 2b-b | Expiry: envelope expires at the configured instant (clock-mocked), notifies sender, and tokens stop working; reminders fire on day 1, day 3 and the day before expiry exactly once each. |
| 2b-c | Void with reason ends the envelope, invalidates all tokens, notifies all parties and is recorded in the hash chain; reassign records old and new signer and re-issues a token. |
| 2b-d | Security: a used single-session token cannot be reused after completion; a forwarded link at auth level ≥ 1 requires the OTP sent to the original recipient; an expired token returns a neutral error page with no document content; a cross-tenant request for another company's envelope id returns 404 with no timing/content leak. |
| 2b-e | Tampered-PDF detection: modifying one byte of a sealed PDF causes the standalone verifier and the verification page to report "modified" and name the failing hash. |
| 2b-f | Audit chain: altering any stored event breaks verification of the chain from that event onward (test mutates the DB directly). |
| 2b-g | Visual regression: signer UI snapshots on iPhone-class, tablet and desktop viewports match baselines within tolerance; axe reports zero serious violations on the signer flow. |
| 2b-h | End-to-end: create a template with two roles in sequence (level 1 OTP) → send → signer 1 OTP → sign → signer 2 OTP → sign → sealed PDF with Certificate of Completion → `tools/sign-verify` validates PAdES signature, RFC 3161 timestamp and audit chain → CRM opportunity marked won and order confirmed. |
| 2b-i | GDPR erasure of a signer removes personal data from envelope records while the evidence hashes and event log remain verifiable. |
| 2b-j | Log scan: grep of all log output during the e2e run finds no document text, field values or signature images. |
| 2b-k | Legal-evidence note and disclaimer published in `docs/sign/legal-evidence.md`; signer help text available in ≥ 2 languages. |
| 2b-l | Risky-clause flags never change envelope state and are labelled "advisory — not legal advice" in UI and API payloads. |

---

## 5. Phase 3 — Supply chain and manufacturing

Modules: `uom`, `purchase`, `inventory`, `inventory_barcode`, `inventory_delivery`, `manufacturing`, `quality`, `maintenance`, `fleet`, bridges `sign_purchase`, `account_inventory` (valuation entries), `sale_inventory`, `purchase_inventory`.

**Scope**
- *Purchase*: vendors and vendor pricelists, RFQs, purchase orders, purchase agreements (blanket/call-off), approval thresholds, three-way matching (PO ↔ receipt ↔ bill), landed costs, vendor performance (on-time, quality, price).
- *Inventory*: warehouses; multi-step receipts, deliveries, internal transfers; location tree, routes, putaway and removal strategies (FIFO/LIFO/FEFO); units of measure with conversions; lots, serials, expiry, packages; valuation (standard, FIFO, AVCO) with automatic journal entries; moves, adjustments, cycle counts, scrap, reorder rules and replenishment; dropshipping, consignment, returns, backorders; barcode flows (receive, pick, pack, ship, count) mobile-first with camera and USB scanners; carrier adapter interface (labels, tracking) with a mock and one real carrier.
- *Manufacturing*: multi-level BoMs (phantom kits, by-products), routings and work centres, manufacturing orders, work orders with time tracking, shop-floor screen (tablet), subcontracting, MRP planning, production costing.
- *Quality*: control points, checks (pass/fail, measurement, photo), alerts, corrective actions.
- *Maintenance*: equipment, preventive schedules, corrective requests, teams, calendar.
- *Fleet*: vehicles, contracts, service logs, fuel, odometer, cost tracking.

**AI features**

| Module | Feature | Tier | Write/outbound? | Threshold |
|---|---|---|---|---|
| purchase | RFQ drafts from demand signals | mid | write → proposal | line accuracy ≥ 0.85 vs planner labels |
| purchase | Supplier recommendations | small | no | top-3 includes chosen ≥ 0.7 |
| purchase | Late-delivery prediction | small + tabular | flag | AUC ≥ 0.75 |
| purchase | Bill-to-PO mismatch explanations | mid | no | 100% numeric consistency; judge ≥ 4/5 |
| inventory | Demand forecasting | stats + mid | no | MAPE ≤ 25% on back-test |
| inventory | Reorder suggestions | small | write → proposal | stock-out reduction ≥ 20% in simulation |
| inventory | Vision-based receiving checks | vision / mid | flag → proposal | discrepancy detection recall ≥ 0.8 |
| inventory | FEFO expiry-risk alerts | small | notification | precision ≥ 0.9 |
| manufacturing | Production scheduling optimisation | reason (+ solver) | write → proposal | makespan ≤ baseline on benchmark set |
| manufacturing | Yield prediction | tabular | no | MAE within tolerance per product family |
| manufacturing | BoM cost-change explanations | mid | no | 100% numeric consistency |
| quality | Defect detection from images | vision | flag | recall ≥ 0.85 on labelled images |
| quality | Root-cause suggestions; control-point drafts from specs | mid | write → proposal | judge ≥ 4/5; spec coverage ≥ 0.8 |
| maintenance | Predictive maintenance from logs/sensors; auto request creation from anomalies | small + stats | create request → proposal (auto-draft allowed) | precision ≥ 0.7; lead time ≥ 24 h on fixtures |
| fleet | Fuel/cost anomaly detection; service prediction; vehicle allocation; receipt extraction | small | flag / proposal | anomaly precision ≥ 0.8; receipt F1 ≥ 0.9 |

**Acceptance criteria**

| # | Criterion |
|---|---|
| 3-a | Three-way match: PO 100 units, receipt 90, bill 100 → bill blocked with a mismatch explanation; receiving the remaining 10 unblocks. |
| 3-b | Valuation: FIFO, AVCO and standard costing on the 30-move fixture match the golden ledger to the cent and post the expected journal entries via `account_inventory`. |
| 3-c | FEFO removal picks the earliest-expiring lot across sub-locations; expired lots are never proposed for delivery. |
| 3-d | Barcode receive→pick→pack→ship flow completes on a 375 px-wide viewport using camera scanning (Playwright with injected barcodes) and USB wedge input, offline for the pick step with queued sync. |
| 3-e | MRP run on the demo BoM tree (3 levels, one phantom kit, one by-product) generates the expected MOs and POs respecting lead times and reorder rules. |
| 3-f | Work-order time tracking from the shop-floor screen rolls up into production cost; subcontracted component flow produces the correct moves. |
| 3-g | Quality check failure raises an alert and blocks the move until a corrective action is recorded. |
| 3-h | Preventive maintenance schedule generates requests on the calendar; a sensor anomaly fixture creates a draft request as a proposal. |
| 3-i | Fleet cost report per vehicle equals the sum of fuel, service and contract costs in the fixture. |
| 3-j | Inventory list and stock-level pivots on 100k moves < 300 ms / < 500 ms p95. |
| 3-k | Carrier mock produces a label PDF and tracking number; tracking webhook updates the delivery state. |

---

## 6. Phase 4 — Operations and field services

Modules: `project`, `timesheet`, `helpdesk`, `field_service`, `planning`, `appointment`, bridges `sign_project`, `sale_project` (billable milestones), `sale_timesheet`.

**Scope**
- *Project*: projects, tasks with dependencies and subtasks, stages, kanban, **Gantt renderer (D-010)**, milestones, recurring tasks, profitability analysis, customer sharing via portal.
- *Timesheets*: timers, time on tasks/projects, approvals, billable/non-billable, invoicing of billable time.
- *Helpdesk*: channels (email, portal, chat, phone), teams, SLA policies with escalation, ticket types, canned responses, knowledge link, satisfaction surveys.
- *Field Service*: dispatch board, technician schedules and routes, **map renderer (D-010)**, work orders, parts used, signature on completion, geolocation check-in; mobile-first.
- *Planning*: shifts and resources, templates, open shifts, availability.
- *Appointments*: booking pages, availability rules, reminders, resource allocation.

**AI features**

| Module | Feature | Tier | Write/outbound? | Threshold |
|---|---|---|---|---|
| project | Task breakdown from a brief | mid | create tasks → proposal | judge ≥ 4/5; coverage ≥ 0.8 vs PM labels |
| project | Effort estimates from past projects | small + stats | write → proposal | MAPE ≤ 30% |
| project | Schedule risk alerts | small | notification | precision ≥ 0.7 |
| project | Status report drafts | mid | outbound → proposal | judge ≥ 4/5; 100% numbers from records |
| timesheet | Narrative drafts; missing/overlapping time detection; billable classification | small | write → proposal | overlap recall 100%; billable accuracy ≥ 0.85 |
| helpdesk | Triage and prioritisation; sentiment-based priority | small | write → proposal (auto-apply by rule allowed) | category accuracy ≥ 0.85 |
| helpdesk | Suggested replies from knowledge base | mid + retrieve | outbound → proposal | judge ≥ 4/5; 100% citations resolvable |
| helpdesk | Auto-resolution of common questions with escalation | mid | outbound only when rule allows; else proposal | resolution precision ≥ 0.9; escalation on low confidence 100% |
| helpdesk | Agent assist with customer history | retrieve | no | judge ≥ 4/5 |
| field_service | Route optimisation; skill matching; duration estimates; parts prediction | reason/small | write → proposal | route ≤ baseline; skill match accuracy ≥ 0.9; duration MAPE ≤ 25% |
| planning | Shift generation from demand forecasts; overtime/fatigue alerts | mid | write → proposal | coverage ≥ 0.95 of demand in simulation; 0 alerts missed on fixture |
| appointment | Smart slot suggestions; no-show prediction; personalised reminders | small | reminders → proposal/template | slot acceptance ≥ 0.7; AUC ≥ 0.7 |

**Acceptance criteria**

| # | Criterion |
|---|---|
| 4-a | Gantt view renders 2,000 tasks with dependencies < 1 s; dragging a task shifts dependents and writes through the ORM with tracking. |
| 4-b | Timesheet overlap detection flags 100% of seeded overlaps; billable hours invoice from the sales order at the correct rate. |
| 4-c | SLA policy escalates a ticket at the configured breach instant (clock-mocked) and notifies the team lead; satisfaction survey is sent on close. |
| 4-d | Helpdesk email channel creates tickets from the alias and threads replies; portal channel and live-chat stub create tickets with the right team. |
| 4-e | Map view shows technician positions and work orders; the dispatch board drag assigns and reschedules; geolocation check-in stores coordinates and time. |
| 4-f | Signature on completion uses the `sign` component and attaches a sealed PDF to the work order. |
| 4-g | Planning: open shifts are claimable by qualified resources only; availability conflicts are refused. |
| 4-h | Appointment booking page honours availability rules and buffers; reminders go out at the configured offsets. |
| 4-i | Project profitability equals revenue (invoiced timesheets + milestones) minus costs (timesheet cost + expenses) on the fixture. |
| 4-j | All auto-resolution responses contain a visible escalation path and are blocked when confidence < threshold (test on low-confidence fixtures). |

---

## 7. Phase 5 — People (HR)

Modules: `hr`, `recruitment`, `time_off`, `attendance`, `expense`, `appraisal`, `payroll`, bridges `sign_hr`, `hr_timesheet`, `payroll_account`.

Employment-related AI features (recruitment, appraisals, attendance analytics, workforce analytics) are **high-risk** under the compliance registry: each requires a documented bias test on a representative synthetic set, a human-decision requirement (no automatic adverse outcome), and a model card in `docs/ai/high-risk/<feature>.md` describing purpose, data, limitations, oversight. Protected attributes are excluded from model inputs and asserted by test.

**Scope**
- *Employees*: profiles, org chart, departments, job positions, skills and certifications, contract history, documents, role-restricted private data (field groups + separate `hr.private_info` model).
- *Recruitment*: job postings published to the website (Phase 7 bridge; Phase 5 ships a standalone public page), applicant pipeline, resume parsing, interview scheduling and scorecards, offers, refusals, candidate→employee conversion.
- *Time off*: leave types, accrual plans, allocations, manager approvals, public holidays, calendar view, balance reports.
- *Attendance*: check-in/out by kiosk, web, mobile, geofence; overtime rules; reports.
- *Expenses*: claims, receipt OCR, policies, mileage, multi-currency, approvals, reimbursement via payslip or bank transfer.
- *Appraisals*: goals, 360° feedback, review cycles, objective tracking.
- *Payroll*: salary structures, rules engine, payslip batches, country-specific tax and social contribution packs (ship a generic pack plus one country), payslip PDFs, bank payment files (SEPA pain.001 + one domestic format).

**AI features**

| Module | Feature | Tier | Risk | Write/outbound? | Threshold / obligations |
|---|---|---|---|---|---|
| hr | Skills-gap analysis; onboarding checklists; org-chart Q&A grounded in accessible records | mid + retrieve | limited | checklist → proposal | Q&A: 0 answers using records outside the asker's access; judge ≥ 4/5 |
| recruitment | Resume parsing | extract / small | high | write → proposal | field F1 ≥ 0.9; no protected-attribute extraction (asserted) |
| recruitment | Candidate matching with bias testing | small | high | ranking shown to human only; no auto-reject | disparate-impact ratio within 0.8–1.25 on synthetic set; human decision required |
| recruitment | Interview question drafts; job descriptions; candidate message drafts | mid | high (messages) / limited | outbound → proposal | judge ≥ 4/5; bias lexicon check 100% |
| time_off | Manager summaries; policy Q&A; coverage-conflict detection | small | limited | no | conflict recall 100% on fixture |
| attendance | Missing check-out / irregular pattern detection; overtime summaries | small | high (analytics) | flag for human review only | precision ≥ 0.8; no automatic sanction path |
| expense | Receipt OCR and categorisation; policy violation flags; duplicate claim detection | extract / small | limited | write → proposal | F1 ≥ 0.9; duplicate recall ≥ 0.95 |
| appraisal | Summary drafts from feedback; goal suggestions | mid | high | write → proposal; manager must edit/accept | bias test on synthetic set; judge ≥ 4/5 |
| payroll | Anomaly detection before each run; payslip Q&A; tax-change impact explanations | small/mid | limited | flag / no | anomaly recall ≥ 0.9; Q&A numeric consistency 100% |

**Acceptance criteria**

| # | Criterion |
|---|---|
| 5-a | Private employee data (salary, bank, national id) is invisible to users outside `hr.group_private` in UI, API, export, search index and AI retrieval (U-5 extended). |
| 5-b | Org chart renders 1,000 employees < 1 s; Q&A about a colleague's salary by a non-HR user returns a refusal with no leak. |
| 5-c | Recruitment pipeline: resume upload → parsed fields proposed → recruiter accepts; candidate matching UI shows scores with factors and a required human decision; no API path auto-rejects. |
| 5-d | Bias test report for matching and appraisal features is generated by `tessera ai bias-test` and stored under `docs/ai/high-risk/`; CI fails if the disparate-impact ratio leaves the band. |
| 5-e | Time off: accrual plan accrues monthly (clock-mocked); approval flow and public holidays exclude correctly; balances report matches the ledger. |
| 5-f | Attendance kiosk check-in on a tablet works offline and syncs; geofence check-in outside the fence is refused. |
| 5-g | Expense OCR eval set meets thresholds; a duplicate receipt is flagged; reimbursement via payslip appears on the next batch. |
| 5-h | Payroll: the generic pack computes the 25-case golden payslips to the cent; pain.001 file validates against the XSD; payslip PDFs render with company layout. |
| 5-i | Offer letter sent via `sign_hr` attaches the sealed PDF to the employee record restricted by role. |
| 5-j | Every high-risk feature has a model card and a human-decision checkpoint verified by e2e. |

---

## 8. Phase 6 — Marketing and engagement

Modules: `email_marketing`, `marketing_automation`, `sms` (campaign features extending the Phase 2b transport module — D-022), `social`, `events`, `survey`, `elearning`, `membership`, `donations`, `livechat`, `blog`, `forum`. GDPR consent records are a `contacts` concern (consent per partner and channel) that `email_marketing` and `sms` read; public pages for events, courses, blog and forum render through `portal` in this phase and move into the website builder via `*_website` bridges in Phase 7 (D-023).

**Scope** (compressed from spec §4 Phase 6): drag-and-drop email builder, templates, lists, filter segmentation, A/B tests, bounce/unsubscribe handling, GDPR consent, open/click analytics; multi-step automation (wait/branch/send/update) triggered by CRM events with UTM and link tracking; SMS marketing and transactional SMS via provider adapters (mock + one real); social post scheduling and analytics across networks via adapters; events with ticket types, registration forms, QR check-in, reminders, event pages, sponsors; surveys with all question types, logic, scoring, invitations, results, certifications; eLearning courses, lessons, quizzes, certificates, progress, paid courses; membership products with auto-renewal and member portal, recurring donation forms; live chat with visitor tracking, chatbot rules and operator routing; blog and forum with moderation.

**AI features**

| Module | Feature | Tier | Write/outbound? | Threshold |
|---|---|---|---|---|
| email_marketing | Copy and subject-line variants; personalised content blocks | mid | outbound → proposal | judge ≥ 4/5; spam-score check passes |
| email_marketing | Send-time optimisation; natural-language segmentation | small | segmentation → domain for review | lift ≥ 5% in back-test; domain accuracy ≥ 0.85 |
| marketing_automation | Journey suggestions and branching recommendations | mid | write → proposal | judge ≥ 4/5 |
| sms | Message drafts; opt-out compliance checks | small | outbound → proposal; non-compliant blocked | compliance recall 100% on fixture |
| social | Post drafts per network; best-time suggestions; reply drafts; brand-guarded image generation | mid / image | outbound → proposal | judge ≥ 4/5; brand-guard rejection of off-brand prompts 100% on fixture |
| events | Attendee matching; session recommendations; sponsor ROI summaries; follow-up drafts | small/mid | outbound → proposal | judge ≥ 4/5; numeric consistency 100% |
| survey | Theme extraction and sentiment on open text; question-design suggestions | small | no / proposal | theme agreement ≥ 0.7 κ with human coding |
| elearning | Course outlines and quizzes from source documents; grounded tutor; adaptive paths | mid + retrieve | write → proposal | 0 tutor answers outside course content; quiz validity judge ≥ 4/5 |
| membership / donations | Member churn prediction; donor propensity scoring; thank-you drafts | small | outbound → proposal | AUC ≥ 0.7; explanations present |
| livechat | Grounded website assistant with human handoff and full transcript | mid + retrieve | outbound to visitor when rule allows; handoff always available | 100% handoff on request or low confidence; 0 answers outside grounded sources |
| blog / forum | Article outlines from topic gaps; moderation flags; thread summaries | small/mid | flags only — never auto-remove | moderation recall ≥ 0.8; 0 automatic removals |

**Acceptance criteria**

| # | Criterion |
|---|---|
| 6-a | A mailing to a 10k-contact list renders personalised content, honours unsubscribe and consent records (0 sends to opted-out contacts), and records opens/clicks via tracking pixel and redirect links. |
| 6-b | A/B test picks the winner at the configured sample size and sends the remainder. |
| 6-c | Automation journey (CRM stage change → wait 2 days → branch on open → SMS) executes on clock-mocked fixtures exactly once per contact. |
| 6-d | SMS adapter mock reports delivery; a message to an opted-out number is blocked with an audit row. |
| 6-e | Event: registration with ticket types, QR issued, check-in by scanning updates attendance in realtime; reminders go out at offsets. |
| 6-f | Survey with conditional logic and scoring produces certificates when thresholds are met; results aggregate correctly. |
| 6-g | eLearning: progress tracking, quiz grading and paid-course checkout via `payment` work end to end; tutor answers cite lesson content only. |
| 6-h | Live chat: visitor → bot rules → operator routing → handoff with full transcript delivered to the operator and stored on the ticket/lead. |
| 6-i | Forum moderation flags are queued for a human; no content is hidden without a moderator action (asserted). |
| 6-j | Membership auto-renewal creates the invoice and payment attempt; failed payment flags the member and drafts (not sends) an outreach. |

---

## 9. Phase 7 — Website, eCommerce and POS

Modules: `website` (includes forms → CRM records), `ecommerce` (includes reviews and wishlists), `pos` (includes loyalty and gift cards), `pos_restaurant`, bridges `recruitment_website`, `events_website`, `elearning_website`, `blog_website`, `forum_website` (D-023).

**Scope**
- *Website builder*: drag-and-drop pages and blocks, themes, SEO controls (meta, sitemap, robots, canonical, hreflang), multi-website and multilingual, cookie consent, forms creating CRM records, custom domains with SSL (ACME integration documented; Compose uses a local CA).
- *eCommerce*: catalogue with variants/attributes, faceted search, reviews, wishlists, carts, guest and returning checkout, shipping rules, coupons, payment gateways (Stripe, PayPal adapters + mock), abandoned-cart emails, B2B pricing (pricelists from `sale`), customer accounts.
- *POS*: terminal UI, multiple payment methods and terminal adapters, cash sessions and reconciliation, **offline mode**, receipts (print/email), restaurant mode (floor plans, kitchen display), loyalty and gift cards, self-order kiosks.

**AI features**

| Module | Feature | Tier | Write/outbound? | Threshold |
|---|---|---|---|---|
| website | Page copy and layout suggestions; SEO metadata; alt-text drafts; translation with human review | mid/small | write → proposal | judge ≥ 4/5; alt-text accuracy ≥ 0.85; 0 auto-published translations |
| ecommerce | Product descriptions; SEO metadata | mid | write → proposal | judge ≥ 4/5; 0 fabricated specs (must come from attributes) |
| ecommerce | Shopping assistant grounded in catalogue; semantic search | mid + retrieve | no (adds to cart only on explicit visitor action) | 0 answers about non-catalogue products; search NDCG@10 ≥ baseline + 10% |
| ecommerce | Personalised recommendations; review summaries; pricing suggestions within guardrails | small | pricing → proposal; never outside band | CTR lift ≥ 5% in back-test; 100% within band |
| pos | Product recognition from images; upsell prompts | vision/small | cashier confirms | top-1 accuracy ≥ 0.9 on catalogue photos |
| pos | Cash-session variance explanations | mid | no | 100% numeric consistency |
| pos_restaurant | Kitchen demand prediction | stats | no | MAPE ≤ 25% |

**Acceptance criteria**

| # | Criterion |
|---|---|
| 7-a | Two websites on different hosts serve different themes, languages and catalogues from one database; sitemap, robots, canonical and hreflang validate. |
| 7-b | A website form creates a CRM lead with UTM data and triggers automations; cookie consent blocks tracking scripts until accepted. |
| 7-c | Checkout as guest and as returning customer with Stripe and PayPal mocks completes, creates the order, reserves stock (Phase 3 bridge) and sends the confirmation; abandoned-cart email fires after the configured delay. |
| 7-d | Faceted search over 50k variants returns < 300 ms p95; semantic search returns relevant results for 20 natural-language queries (golden set). |
| 7-e | POS offline: with the network disconnected, 50 sales are completed, receipts printed/emailed on reconnect, and the session reconciles to the cent; no duplicate orders after sync. |
| 7-f | Restaurant: floor plan table transfer and split bill; kitchen display receives orders in realtime by course. |
| 7-g | Loyalty points and gift cards apply correctly across POS and eCommerce. |
| 7-h | Self-order kiosk flow completes on a 1080×1920 touch viewport with axe zero serious violations. |
| 7-i | Lighthouse performance ≥ 85 and accessibility ≥ 95 on the demo storefront home and product pages. |
| 7-j | Shopping assistant never recommends an out-of-catalogue or out-of-stock product on the adversarial fixture. |

---

## 10. Phase 8 — Knowledge, documents and administration

Modules: `documents`, `knowledge`, `approvals`, `todo`, `notes`, `dashboards`, `spreadsheet`, `marketplace`, `iot`, `assistant`, `ai_admin`, `studio_ai`, `reports_editor` (full visual editor), bridges `sign_documents`, `sign_knowledge`, `sign_approvals`.

**Scope**
- *Documents*: folders, tags, versioning, workspaces, OCR, auto-filing rules, send for signature.
- *Knowledge*: wiki articles, templates, collaborative editing (CRDT), internal and portal access, policy acknowledgements with due dates.
- *Approvals*: request types, multi-level approvals, amount-based rules, notifications, optional signature step.
- *To-do and notes*.
- *Dashboards and BI*: custom drag-and-drop dashboards, saved reports, exports, embedded charts, scheduled report emails.
- *Spreadsheet*: collaborative spreadsheet linked to live data, formulas, pivot tables.
- *Marketplace*: module store with versioning, compatibility checks, install from registry.
- *IoT (optional)*: device registry, MQTT ingestion, sensor data linkage.
- *Assistant*: cross-module copilot in the command palette, bound by permissions.
- *AI admin*: usage, cost, adoption and quality dashboards; prompt and eval management UI.
- *Studio AI*: natural-language creation of fields, views, automations with preview-before-apply.
- *Reports editor*: full visual PDF template editor.

**AI features**

| Module | Feature | Tier | Write/outbound? | Threshold |
|---|---|---|---|---|
| documents | Classification; field extraction; semantic search; Q&A with citations | small/mid + retrieve | filing → proposal | classification ≥ 0.9; extraction F1 ≥ 0.9; 100% citations resolvable |
| knowledge | Article drafts from resolved tickets; stale/duplicate detection | mid | write → proposal | judge ≥ 4/5; duplicate precision ≥ 0.85 |
| approvals | Approver recommendations; request summaries; policy flags | small | no / flag | approver accuracy ≥ 0.8 |
| todo / notes | Task extraction from notes and chatter; meeting summaries | small | create tasks → proposal | F1 ≥ 0.85 |
| dashboards | Natural-language chart creation; narrative insights; KPI anomaly alerts | mid | chart spec for review | spec accuracy ≥ 0.85; numbers from engine only |
| spreadsheet | Formula help; NL → formula; data-cleaning suggestions | small | cell edits → preview/accept | formula correctness ≥ 0.9 |
| marketplace | Compatibility and risk checks; advisory industry recommendations | small | no | manifest-check recall 100%; advisory label present |
| iot | Sensor anomaly detection; predictive alerts | stats/small | notification | precision ≥ 0.8 |
| studio_ai | NL creation of fields, views, automations — preview, then apply | mid | **preview → explicit apply** | generated spec validity 100% (schema); intent accuracy ≥ 0.85 |
| assistant | Cross-module copilot | mid + tools + retrieve | all writes → proposal | 0 permission violations on corpus; judge ≥ 4/5 |
| ai_admin | Usage, cost, adoption, quality dashboards | — | no | dashboards tie to ledger to the cent |

**Acceptance criteria**

| # | Criterion |
|---|---|
| 8-a | Documents: upload → OCR → classification and auto-filing proposal → accept; versioning keeps every version; send for signature from the document. |
| 8-b | Knowledge collaborative editing: two browsers edit the same article concurrently without lost updates (CRDT test); policy acknowledgement tracking shows completion by due date. |
| 8-c | Approvals: a 3-level, amount-based flow routes correctly on the fixture; the signature step produces a sealed PDF. |
| 8-d | Dashboards: drag-and-drop layout persists per user; scheduled report email arrives with the PDF/XLSX; NL chart "revenue by team this quarter" produces a reviewable chart spec whose numbers equal the pivot. |
| 8-e | Spreadsheet linked to live `crm.lead` data recalculates on data change; pivot table matches `read_group`. |
| 8-f | Marketplace installs a module from the local registry, refuses an incompatible version with a clear message, and upgrades with migration logs. |
| 8-g | Studio AI: "add a priority field to helpdesk tickets and show it on the kanban" produces a preview diff; nothing changes until Apply; rollback works. |
| 8-h | Global assistant corpus (200 prompts across modules, including injections): 0 permission violations, 0 writes without proposals, every factual answer cites records the user can open. |
| 8-i | AI admin dashboards show per-feature adoption, acceptance/rejection, cost, latency and eval scores; kill switches and flags are editable there. |
| 8-j | IoT: MQTT fixture device publishes readings that appear on equipment within 2 s and trigger a maintenance anomaly flag. |

---

## 11. Cross-phase tracks

| Track | Continuous obligations | Phase-specific additions |
|---|---|---|
| **Security (ASVS L2)** | Checklist delta reviewed per phase; dependency scanning; secret scanning; authz tests for every new model; rate-limit tests; no high-severity finding open at phase exit. | P1: auth, sessions, CSRF, API keys, 2FA, OIDC, prompt-injection and tool-permission suites. P2: payment adapters, SAML, accounting lock integrity. P2b: signer tokens, PAdES, cross-tenant envelope tests, log content scan. P5: private-data isolation, high-risk AI obligations. P7: public-facing surfaces (CSP, SSRF on builder, storefront abuse limits). |
| **Performance** | Nightly 100k-record seed and timing assertions (< 300 ms list p95, < 500 ms pivot p95); `EXPLAIN` regression checks; AI interactive ≤ 3 s p95. | P3: 100k stock moves; P5: 1,000-employee org chart; P7: 50k-variant storefront search; P8: dashboards with 20 widgets. |
| **Accessibility** | axe in Playwright for every new view/page; zero serious/critical; keyboard-only run per module. | P2b signer UI, P7 storefront/POS/kiosk and P6 public pages get manual screen-reader passes. |
| **i18n** | `en_US` + `fr_FR` seeded from Phase 1; all strings translatable; RTL layout check from P1; per-user tz tests. | P2: country CoA templates; P5: payroll country packs; P7: multilingual websites and hreflang. |
| **Documentation** | OpenAPI regenerated; admin guide and per-module developer guide; `CHANGELOG.md`; `DECISIONS.md`; AI governance doc updated with new features, model choices, risk classes and limitations. | P2b legal-evidence note; P5 high-risk model cards; P8 marketplace publishing guide. |
| **AI evaluation** | Replay evals on every PR; nightly live run; drift alerts when live score < threshold; feedback export to eval sets monthly. | P5 bias-test job; P8 AI admin quality dashboards. |
| **Integrations (spec §5)** | Adapter interfaces with a mock and contract tests before any real adapter. | P1: SMTP/IMAP, Gmail and Microsoft 365 mail, Google/Microsoft/CalDAV calendar, AI providers, speech-to-text (call summaries), Zapier/Make webhooks. P2: payment gateways (Stripe), tax/e-invoicing (Peppol), bank feeds (file formats; live feed adapter interface). P2b: SMS (OTP), ID-verification and TSP adapters. P3: carriers, OCR for receipts. P4: Slack/Teams notifications, geolocation/maps. P6: SMS marketing, social networks. P7: PayPal, ACME/SSL, payment terminals. P8: Google Drive/OneDrive, MQTT. |

---

## 12. Risks and mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | **Kernel complexity** — the metadata ORM, extension composition and schema sync are the critical path; defects propagate everywhere. | Sub-phase 1.1 is kernel-only with property-based tests (1.1-c), ≥ 85% coverage and mypy strict; a reference test module pair exercises extension before any business module; kernel API frozen behind a documented contract after 1.1 with deprecation policy. |
| 2 | **Metadata ORM performance** — generic SQL generation can produce slow queries. | Batched prefetch, declared indexes, `read_group` pushdown, `EXPLAIN` tests in CI from 1.1, nightly 100k perf suite (U-6), query-plan budget per standard view, keyset pagination. |
| 3 | **AI provider drift and cost** — APIs change, prices change, outages happen. | Adapter contract tests with recorded fixtures; routing with fallback and circuit breakers; per-company budgets and quotas; response cache; prompt registry decoupled from code; nightly live evals detect drift; model IDs are configuration, not code. |
| 4 | **Evaluation quality** — thresholds that are easy to pass give false confidence. | Minimum set sizes; mixed human-labelled and judge-scored metrics; adversarial and injection corpora; feedback export grows sets monthly; thresholds reviewed each phase and recorded in DECISIONS. |
| 5 | **Sign legal and cryptographic correctness** — PAdES, RFC 3161 and evidence handling must be right. | Use established libraries (pyHanko-class) rather than custom crypto; standalone verifier independent of the app; tamper and chain tests (2b-e, 2b-f); legal-evidence note with explicit "not legal advice" disclaimer; external legal review recommended before production use. |
| 6 | **Accounting correctness** — ledger errors are costly and hard to detect. | Golden-set driven development (tax, FX, valuation, payroll); invariants asserted in tests (trial balance zero, period lock immutability); double-entry enforced at the ORM layer; reports reconcile to ledger cross-checks. |
| 7 | **Scope size** — the suite is very large. | Strict phase/sub-phase exit criteria; no module without tests and an AI feature; "stubbed/known gaps" summary makes trade-offs explicit; adapter interfaces with mocks let real integrations land later without redesign. |
| 8 | **Generic renderer performance on the frontend** — one renderer for every model can be slow on large forms/lists. | Virtualised lists, field-widget lazy loading, memoised view compilation, measured in Playwright perf tests from 1.2; module-specific widgets allowed through the client-extension registry when generic rendering is insufficient. |

---

## 13. Approval checklist

By approving Phase 1 you are approving:

1. **Stack and kernel strategy** — FastAPI + custom metadata ORM on SQLAlchemy Core/psycopg 3 (D-002), which is hard to reverse after sub-phase 1.1.
2. **Branding** — "Tessera" as the working name (D-001), renameable via script through Phase 1.
3. **Monorepo layout and artefact formats** — TOML manifests, JSON views/menus/security/data, YAML prompts, `.po` translations (D-003, D-004).
4. **Domain DSL** — nested `all`/`any`/`not` JSON (D-005).
5. **Phase 1 scope** — sub-phases 1.0–1.7 (D-021) including minimal `product` and `sale` quotations (D-011), Studio v1 (D-012), OIDC before SAML (D-013), Gantt/map renderers deferred to Phase 4 (D-010); and the module layering in the dependency map, including `sms` transport in Phase 2b (D-022) and `*_website` bridges (D-023).
6. **AI topology** — separate gateway service plus in-process AI runtime (D-014), pgvector retrieval (D-015), default model tiers (D-016), replay-mode CI with nightly live evals (D-017).
7. **Definition of done** — §1.2–§1.4 universal criteria and the AI platform gates, applied to every phase.
8. **Licence** — Apache-2.0 (D-018), changeable before first external release.
9. **Phase 1 acceptance criteria** — §2.0–§2.7 as the exit tests for Phase 1.

Items that can be changed later without rework: brand name, licence, model tiers and provider defaults, eval thresholds, which real integration adapters ship first.
