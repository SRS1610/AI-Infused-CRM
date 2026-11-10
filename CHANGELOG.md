# Changelog

All notable changes are recorded here, grouped by phase. Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow the platform version, modules carry their own versions in their manifests.

## [Unreleased] — Phase 1 complete, awaiting sign-off

Summary of what works, what is stubbed and the known gaps: [docs/phase-1-summary.md](docs/phase-1-summary.md).

### Sub-phase 1.7 — Studio, demo and phase summary
- Studio (`studio`): add fields of every type to Studio-enabled models without a restart (per-database
  dynamic fragments and a registry stamp, D-053), place them on the form and list, edit the form layout,
  automated actions and simple workflows, change history with rollback per change or to a snapshot,
  export as an installable `studio_customizations_<company>` module (D-054); designers only (403
  otherwise). User guide: `docs/user/studio.md`.
- `tessera seed demo --phase 1` prepares the demo scenario in one command: modules with demo data,
  retrieval index, trained scoring models, risk and forecast, realistic answers for the test provider
  (D-055). The walkthrough `docs/demos/phase-1.md` runs in the browser as `frontend/e2e/demo.spec.ts`.
- Client internationalisation: gettext-style `t()` / `tn()`, an extractor script and a complete French
  catalogue (645 strings).
- Fixes found by the end-to-end suite: registry rebuilds are staged and published in one step, and a
  released savepoint hands its rollback callbacks to the enclosing level (D-060); API rate limits are
  per credential with a per-address ceiling, 429 responses carry `Retry-After` and the client retries
  after it (D-059); the in-process gateway is imported at startup, so the first AI request after a
  restart meets the first-token budget; accessibility fixes in the chatter and Studio.
- Uninstalling a module renames its indexes and many-to-many tables instead of leaving them behind
  (D-052).
- Tests: backend 435, gateway 118, frontend 119, Playwright 38 scenarios on desktop and mobile; all 21
  Phase 1 AI features meet their thresholds in replay.

### Sub-phase 1.6 — Phase 1 AI features
Twenty-one features, each with a manifest entry, prompt, kill switch, evaluation set with
prompt-injection cases, replay recordings where a model is called, and tests that nothing changes
until a person accepts the proposal (D-058 explains what the scores measure).
- Discuss: channel and thread summary with cited messages, "Catch me up" across unread channels,
  streamed reply draft, message translation (chrF measured against references).
- Activities: AI-ranked daily to-do with reasons and a morning notification.
- Calendar: meeting brief before each meeting (only facts the attendee may read), suggested meeting
  times, scheduling from plain text into a reviewable event.
- Contacts: duplicate detection (rules, trigram and embeddings) with a merge proposal, tag and parent
  suggestions, enrichment through approved adapters with a source on every value (D-057).
- CRM: per-company lead scoring with the top factors and a "why" panel and no protected attributes,
  next-best-action, deal-risk alerts, lead routing (bulk review), forecast with P10/P50/P90 ranges
  (D-056); lead extraction from intake e-mails and forms, streamed follow-up drafts whose prices must come
  from quotations or products, call-notes summary with per-field proposals and audit trail, objection
  handling with cited past deals, natural-language search turned into a reviewable filter.
- Platform fixes found on the way: notifications open their link; evaluation cases may only watch real
  models (outbound mail is `core.mail_queue`); test-database rebuilds drop the gateway ledger; other
  users see only the audit skeleton of a run; defaults on group-restricted fields no longer block create.
- Client: form side panels (`registerFormPanel`) and search-bar add-ons (`registerSearchAddon`).
- Tests: backend 422 (coverage 87 %), frontend 102 (coverage 90 %), Playwright AI scenarios on desktop
  and mobile (first streamed token under 1.5 s, AI-drafted label until edited, kill switch).

### Sub-phase 1.5 — AI platform
- AI gateway service (`gateway/`): provider adapters for Anthropic, OpenAI, Azure OpenAI, Google Gemini,
  OpenAI-compatible self-hosted servers (vLLM, Ollama), Voyage embeddings and a deterministic mock;
  per-company routing by task class with fallback and circuit breakers; budgets and per-user quotas;
  PII redaction with re-hydration (also in streams); JSON Schema validation with one repair; output
  screening; kill switches; residency and PII-approval policies; response cache; usage and cost ledger;
  SSE streaming; Prometheus metrics; `gateway serve`.
- `ai_core` module and `tessera.ai` runtime: providers, routes, budgets and company settings for
  administrators; feature manifests (`ai/features.yaml`) and a versioned prompt registry; tool framework
  with permission-scoped built-in tools; retrieval over pgvector with access filtering before ranking and
  hybrid ranking; proposals with signed payloads and accept / edit / reject; run, tool-call, citation and
  feedback audit; retention purge; evaluation harness with replay cassettes (`tessera ai eval run|report`,
  `tessera ai classification`, `tessera ai reindex`).
- Kernel: `fields.Vector` with HNSW indexes; uninstall now moves indexes and join tables aside (D-052).
- Client: AI-drafted label, proposal review card, streamed "Draft with AI" in the chatter composer
  (for models that register a draft feature), feedback buttons, "AI unavailable" state, and an
  administrator dashboard for usage, cost, acceptance and kill switches.
- Docs: `docs/ai/governance.md`, `docs/ai/developer-guide.md`, `docs/admin/ai.md`.
- Tests: gateway 118 (coverage 92 %, adapter contract suite on provider-format fixtures); backend 329
  (coverage 87 %, including the 50-case prompt-injection corpus, 200 adversarial retrieval queries and
  deterministic replay evaluations); frontend 66 (coverage 88 %).
- Not yet: no live provider has been called (no keys in this environment); the browser end-to-end check
  of the streamed draft arrives with the first application feature in 1.6.

### Sub-phase 1.4 — Applications
- `contacts`: companies and people with addresses, tags, hierarchy and commercial entity; duplicate
  detection (same email, same phone, same name in the same company) and a merge wizard with field-by-field choices,
  reference re-pointing across all models and a 7-day undo.
- `product` and `sale` (quotation subset): products, quotations with lines, optional lines and taxes,
  workflow-driven states, quotation PDF, email with a signed customer link, online accept/reject,
  automatic expiry.
- `crm`: leads and opportunities, stages, teams with round-robin and rule-based assignment, rule
  scoring with nightly re-score, lead conversion and merge, pipeline kanban with stage totals,
  web-to-lead form with embeddable script, sales analysis and forecast; an accepted quotation marks the
  opportunity won.
- `portal`: invitations for customer contacts, customer home with their documents and quotations;
  customers reach only purpose-built endpoints (D-041).
- `discuss`: channels, direct messages, unread counts, mentions, invitations and presence (D-044).
- `calendar`: meetings with attendees, RSVP (in-app and email link), rooms with conflict checks,
  RFC 5545 recurrence, booking links with free-slot search, `.ics` invitations, two-way sync with
  CalDAV, Google Calendar and Microsoft 365 (D-043).
- Installer creates required PostgreSQL extensions on a fresh database (D-045).
- Client: CRM reports page, merge dialog, customer quotation page, portal app, Discuss app, calendar
  agenda week/day views; mobile form layout no longer overflows beside the chatter.
- User guides in `docs/user/` for every application.
- Tests: 297 backend (coverage 87 %, incl. fresh-database CRM install under 60 s, CalDAV contract tests
  against Radicale), 57 frontend unit (coverage 88 %), 11 new Playwright scenarios on desktop and mobile.

### Sub-phase 1.3 — Platform services
- Background jobs (`tessera.jobs`): PostgreSQL job ledger with per-attempt history, Celery/inline/manual
  dispatch after commit, retries with capped exponential backoff, dead letter and re-queue, idempotency
  keys, progress events, sweep for lost workers; scheduler with exactly-once claiming across replicas;
  `tessera jobs` CLI; webhook deliveries moved onto jobs.
- Mail: outgoing queue with per-company SMTP servers (encrypted passwords) and bounce handling; inbound
  routing by signed `reply+` address, In-Reply-To or alias (`message_new` / `message_update`), via IMAP
  fetcher or HTTP endpoint; email notifications and a daily digest.
- `chatter` module: thread mixin, messages and internal notes, followers and subtypes, field tracking,
  reactions, attachments, @mentions, activities with a per-user dashboard; in-app notifications live
  over WebSocket; Web Push subscriptions stored (sending arrives with the PWA work in 1.7).
- `workflow` module: state machines as data with guarded, group-restricted transitions (409 when not
  allowed), strict state fields, status bar; automations on create/write/delete/time/transition/webhook
  with a sandboxed expression language, run log and once-only time triggers.
- Import/export: CSV/XLSX, mapping suggestions, saved mappings, dry run that writes nothing, background
  import with progress; export through relation paths honouring field access.
- Global search index with access re-check; command palette searches records.
- `reports` module: Jinja2 sandbox → WeasyPrint PDF, per-company layouts, versioned templates, QR codes,
  block editor format and preview.
- Client: chatter panel, status bar, print menu, calendar/pivot/graph views, activity dashboard,
  notification bell, import wizard, export dialog, record route `/m/:model/:id`.
- Security: access-rule cache tied to a transactional stamp (multi-process correct, rollback safe).
- Tests: 238 backend (coverage 85 %, incl. 100k-row pivot p95, PDF/QR decode, real SMTP delivery), 47 frontend
  unit (coverage 88 %), 13 Playwright e2e on desktop and mobile with axe checks.

### Sub-phase 1.2 — API and client foundation
- HTTP API: FastAPI app, session + CSRF auth, TOTP with recovery codes, account lockout, API keys,
  OIDC SSO (PKCE), auto REST (filters, keyset pagination, expand, ETags, method calls), JSON-RPC 2.0,
  attachments, webhooks with HMAC signatures and retry backoff, realtime WebSocket hub.
- `web` module: menus, actions, views as data with inheritance operations and schema validation,
  generated default views, saved filters.
- Web client: login (password/TOTP/SSO), app shell with menus and multi-company switcher, command
  palette, generic list (sorting, grouping, totals, bulk actions, configurable columns, inline editing
  with conflict handling), form (validation, method buttons, live refresh), kanban (drag-and-drop,
  quick create), search panel with condition editor and favourites, all field widgets, dark mode,
  responsive layout, PWA manifest + service worker.
- Tests: 107 backend (coverage 82 %), 34 frontend (coverage 87 %), 7 Playwright e2e (desktop + mobile,
  axe accessibility checks, two-browser realtime).

### Sub-phase 1.1 — Kernel
- Metadata ORM: field types (char, text, html, integer, float, monetary, boolean, date, datetime,
  selection, json, binary, image, many2one, one2many, many2many, reference), computed (stored and
  on-the-fly), related, translatable (JSONB per language) and company-dependent fields; recordsets;
  model extension across modules (`_extend`) with attribute merging and MRO method overrides;
  abstract mixins (`_inherit`); dependency triggers through relations in both directions.
- Domain DSL (nested `all`/`any`/`not` JSON) with variables, in-memory evaluation and SQL
  compilation (sub-selects, `child_of`/`parent_of` recursive CTEs, translatable/company-dependent
  columns, ordering by many2one, `read_group` aggregates with date granularity).
- Declarative schema sync (tables, columns, widening, indexes incl. trigram/unique/partial/composite,
  FKs, m2m join tables, SQL constraints) and drift report (`tessera db check`).
- Module system: TOML manifests, dependency resolution, auto-install, JSON data loader with
  external ids and translations, install/upgrade (versioned `pre`/`post` migrations)/uninstall,
  module records, migration log, scaffold command.
- Security: database-backed provider (groups with implied closure, model access rights, record
  rules with `$uid`/`$companies`/`$user.*` variables, implicit multi-company rule, field groups),
  per-user transaction cache scoping, explicit elevation, Argon2id passwords, authentication.
- `core` module: companies and settings, users and groups, access rights and record rules,
  currencies and dated rates with conversion, countries/states, languages, sequences (gapless and
  standard), attachments on content-addressed object storage, audit log and PII access log,
  module registry, model/field metadata sync.
- Test harness (`conftest.py`, kernel test modules A/B) — 73 tests; ruff clean; mypy per D-025.

### Sub-phase 1.0 — Repository and tooling
- Monorepo scaffold: `backend/` (uv, hatchling), Docker Compose stack (pgvector, Redis, MinIO,
  Mailpit, server, worker, scheduler, gateway, web, optional Ollama), Dockerfiles, GitHub Actions
  CI, Makefile, `.env.example`, pre-commit; Typer CLI skeleton.

### Planning
- Architecture document, module dependency map, core ERD, AI architecture note and phased build plan under `docs/architecture/`.
- `DECISIONS.md` (D-001 … D-031; D-032 … D-040 added in 1.3).
