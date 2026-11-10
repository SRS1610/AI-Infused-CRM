# Changelog

All notable changes are recorded here, grouped by phase. Format follows [Keep a Changelog](https://keepachangelog.com/); versions follow the platform version, modules carry their own versions in their manifests.

## [Unreleased] — Phase 1 in progress

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
- `DECISIONS.md` (D-001 … D-031).
