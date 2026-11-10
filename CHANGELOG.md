# Changelog

All notable changes are recorded here, grouped by phase. Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow the platform version, modules carry their own versions in their manifests.

## [Unreleased] — Phase 1 in progress

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
