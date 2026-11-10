# Changelog

All notable changes are recorded here, grouped by phase. Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow the platform version, modules carry their own versions in their manifests.

## [Unreleased] — Phase 2 (Sales and Accounting), in progress

### Sub-phase 2.1 — Accounting core
- **Ledger (`account` 1.0.0, D-072, D-073, D-077).** Charts of accounts for the US, EU-style VAT and a
  generic chart, installed on every company (and on companies created later) with tax grids, fiscal
  positions and journals; `tessera account charts | install-chart | integrity`. Customer invoices, vendor
  bills, credit notes and journal entries whose tax and payment-term lines follow the invoice lines;
  gapless numbers per journal and fiscal year allocated at posting (`INV/2026/00001`); posted entries
  read-only and never deleted; lock dates for everyone, for non-advisers and for taxes; a PostgreSQL
  constraint trigger refusing unbalanced posted entries; SHA-256 chains on sales and purchase journals.
- **Taxes.** Percentage, fixed, tax-included percentage and group taxes, price-included and cascading
  taxes, invoice / credit-note distribution with tax-report grids, reverse charge; fiscal positions chosen
  from the partner or detected from country and tax ID. A 40-case golden set covers the engine (2-e).
- **Payments.** Payment terms with installments and early-payment discounts; payments registered from
  invoices (partial, batch per partner, write-off, other currency), matching with realised exchange
  differences, month-end unrealised revaluation reversed the next day (2-c); invoices *Not paid*,
  *Partially paid*, *In payment*, *Paid* or *Reversed*.
- **Customers and demo.** Invoice email with PDF and a token link, portal access to posted invoices,
  `tessera seed demo --phase 2` adds invoices, bills, payments and a credit note per company.
- **Forms.** Buttons can ask for parameters (amount, date, reason…) in a dialog before running.
- Guide: [docs/admin/accounting.md](docs/admin/accounting.md).

### Sub-phase 2.2 — Product 2.0
- **Units, variants and pricelists (`product` 2.0.0, D-070, D-071).** Units have a type (reference /
  bigger / smaller, one reference per category) and a rounding precision; quantities and prices convert
  within a category. Attributes and values (with extra prices) turn a product into a template whose
  *Generate variants* creates one child product per combination, archives the ones no longer possible and
  keeps the shared fields in sync (refused when written on a variant); `is_sellable` marks products that
  can go on a line. Pricelists (currency, company, countries) with rules on all products, a category, a
  product or a variant, minimum quantities and dates; fixed, discount or formula prices (rounding,
  surcharge, margins), based on the sales price, the cost or another pricelist, with currency conversion
  and a recursion guard; `_for_partner` picks the partner's, the regional or the company's pricelist.
  New *Catalogue* menu; demo T-shirt variants and *Wholesale* / *Europe (EUR)* lists. `tessera db migrate`
  types existing units.

### Sub-phase 2.4 — Analytic, budgets, assets, reports, tax return
- **Analytic accounting (`account_analytic` 1.0.0).** Analytic plans (shared or per company, optional or
  mandatory by account type, account prefix or journal) and analytic accounts with revenue, costs and
  balance. Invoice lines and journal items carry an analytic distribution (percentages per analytic
  account, at most 100 % per plan), still editable after posting; posting creates analytic items
  (revenue positive, costs negative), reset and cancel remove them, credit notes keep the distribution.
  Distribution models prefill new lines by partner, product, category, account prefix and company;
  mandatory plans block posting. Analytic items list, pivot (account × month) and graph.
- **Budgets (`account_budget` 1.0.0).** Budgets with expense and revenue lines on analytic accounts or
  financial accounts / account types; actual, theoretical (elapsed share), achievement and variance kept
  up to date as items are posted, with a nightly refresh; budget-vs-actual list, pivot and graph; monthly
  split; `variance_report()` service for the 2.6 explanations.
- **Fixed assets (`account_asset` 1.0.0, 2-g).** Assets and asset models; straight-line, declining and
  declining-then-straight-line boards with no prorata, constant (30/360) periods or daily prorata,
  planned as draft entries posted by a daily job (lock dates respected); value decrease (impairment) and
  increase (gross-increase child asset), pause / resume, disposal and sale with gain or loss; assets
  created from vendor bill lines on accounts with an asset model. Golden-table tests for every method.
- Demo: departments and projects with allocated invoices and bills, a confirmed yearly budget, a laptop,
  a van and office furniture with partly posted boards. Guides:
  [analytic](docs/admin/analytic.md), [budgets](docs/admin/budgets.md), [assets](docs/admin/assets.md).

- **Financial reports (`account_reports` 1.0.0, D-077).** General ledger, trial balance, profit and loss,
  balance sheet (computed current-year earnings and retained earnings, so assets = liabilities + equity at
  any date), direct-method cash flow statement, aged receivable / payable (residuals as of the date) and
  partner ledger, aggregated in SQL from journal items; date range or as-of date, fiscal-year aware,
  comparison columns, journal / partner / draft filters. Reports are data (`account.report` and lines with
  `type()` / `code()` / `tag()` / `grid()` / `cash()` / `line()` expressions); consolidation converts
  companies at closing / average rates with a currency translation adjustment. JSON API
  (`/api/v1/account/reports/{code}`), CSV / XLSX / PDF exports, a `ReportParams` object for the 2.6
  natural-language reports, and a report page with drill-down to journal items. 2-a: the demo's trial
  balances sum to zero per company and consolidated, net income equals the balance sheet's current year
  earnings. `tessera perf reports` (2-l): every report under 450 ms p95 on 100,000 journal items; nightly job.
- **Tax returns (`account_tax` 1.0.0).** VAT / sales tax return per chart template (EU, US, generic) from the
  tax grids, signed so that credit notes reduce them, tied to the tax accounts (2-e); tax closing entry per
  tax group into tax payable / receivable with the tax lock date; tax return records (monthly or quarterly)
  with the report snapshot; deterministic pre-filing checks (`prefiling_checks`).
- Guides: [docs/admin/reports.md](docs/admin/reports.md), [docs/admin/tax-return.md](docs/admin/tax-return.md).

### Sub-phase 2.7 — SAML single sign-on
- **SAML 2.0 single sign-on (`auth_saml`, D-013, D-069).** Administrators add SAML identity providers under
  Settings → Users & Companies; the login page offers them next to OIDC. Service-provider initiated login
  (HTTP-Redirect request, optionally signed; HTTP-POST response) with signatures checked against the
  configured IdP certificates only, signature-wrapping and XXE defences, one-use requests bound to the
  browser, an assertion replay cache, and accounts linked by administrator, by trusted e-mail or created on
  first login (off by default). Guide: [docs/admin/saml.md](docs/admin/saml.md).

## [Unreleased] — Phase 1 complete, awaiting sign-off

Summary of what works, what is stubbed and the known gaps: [docs/phase-1-summary.md](docs/phase-1-summary.md).

### Phase 1 completion — acceptance gaps closed
- **Server translations (U-8, D-061).** Everything the server sends is translatable: every module ships
  gettext catalogues (`i18n/<module>.pot`, `i18n/<lang>.po`) and every Phase 1 module has a complete
  French catalogue (2,174 strings). Field labels, help, selection values, model names, view labels, error
  messages, notifications and printed quotations follow the user's language (user setting,
  `X-Tessera-Lang`, or `Accept-Language` before login); menus, actions, stages and groups get their
  translations on install and upgrade. New commands `tessera i18n extract | stats | load`; a test fails
  when a catalogue drops below 95 % or breaks a placeholder.
- **Studio v1 scope (1.7, D-062).** Fields take a widget, a default value, required / read-only conditions
  written as plain rows ("when Stage is Won"; the server refuses a matching record left empty) and the
  groups that may see them; Studio fields can be edited later. Form, list (order, hide, optional columns,
  new columns) and kanban card layouts are edited by drag and drop; new form, list or kanban views start
  from the generated layout and can become the default; search filters and group-bys are edited in place.
  A workflow builder defines states and transitions (from, to, label, condition, group) on a choice or
  link field or a new one. Every change is in the history, rolls back (refusing one a later change relies
  on) and exports. Forms honour field-level read-only and required conditions; the client's French
  catalogue covers all 765 strings.
- **Calendar, Discuss, CRM, Contacts (D-063, D-064).** Google and Microsoft calendar accounts refresh their
  OAuth tokens; an account the provider refuses shows "Needs reconnection" and notifies its owner. With
  the Redis realtime backend, presence covers every API process. Lost opportunities stay in the retrieval
  index with their lost reason, so objection handling learns from them; follow-up drafts also flag prices
  typed without a currency. A keyless "Company website" enrichment source (approval required) suggests
  name, description, phone, address, social profiles and logo from a company's public home page, with
  SSRF protection; companies gain a "Public profile" tab. Calendar and Contacts are version 1.1.0
  (`tessera db migrate`).
- **AI evaluations (D-063).** `tessera ai eval run` runs each feature against its own corpus in a
  rolled-back sandbox, so it scores the same on test, demo and production databases and writes nothing.
  `tessera ai provider` routes the default task classes to a provider with the D-016 tiers; in-process
  mode reads `GATEWAY_<KIND>_API_KEY` like the gateway service.
- **Docker Compose verified (U-1, D-065, D-066).** On a fresh machine `make up` now builds and starts
  every service, `tessera seed demo` runs through the gateway service, and all 74 Phase 1 menus open
  without errors. Fixed on the way: the dev object store is SeaweedFS (MinIO images are gone from Docker
  Hub) and the S3 backend creates its bucket; the web container proxied the API to itself; a process
  started before modules were installed elsewhere now loads them; images pin uv; `.dockerignore`.
- **Security review (U-9, D-067).** OWASP ASVS L2 review of the whole application
  (`docs/security/asvs-phase-1.md`: 209 requirements, no high-severity finding open). Fixed seven
  high-severity issues: company scope widened through the request context, API keys re-pointed at other
  users, attachments readable and shareable outside their record's permissions, uploaded HTML/SVG rendered
  inline, Studio automations granting administrator rights, local files and URLs embedded in PDFs, and
  account takeover through single sign-on with an unverified e-mail. Also hardened: wrong second-factor
  codes count towards lockout, client addresses behind proxies (`TESSERA_TRUSTED_PROXIES`), production
  configuration checks, outbound calls to private addresses (`TESSERA_OUTBOUND_ALLOW_PRIVATE`), a CSP on
  HTML served by the API, JSON-RPC batch size, constant-time login failures.
- **Metrics (U-10, D-068).** `GET /metrics` on the API: request traffic and latency by route, background
  jobs (attempts, outcome, run time) and AI feature runs (outcome, tokens, cost, provider time), plus the
  in-process gateway's metrics; production requires `TESSERA_METRICS_TOKEN`.
- **Performance at volume (U-6, D-065).** `tessera perf lists` fills the primary models to 100k rows and
  times their list views: p95 at most 108 ms (budget 300 ms) as administrator and as a salesperson under
  record rules. Fixed on the way: database sessions lost their statement timeout after first use, and
  PostgreSQL JIT added ~160 ms to list counts (now off for application sessions). A nightly workflow runs
  the perf check and, when the `ANTHROPIC_API_KEY` secret is set, the live AI evaluation.

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
