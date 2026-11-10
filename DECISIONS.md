# DECISIONS.md

Architecture and product decisions made without a blocking question, in the order they were taken. Each entry records the decision, the alternatives considered, the rationale and how hard it is to reverse. Supersede an entry by adding a new one and marking the old one *Superseded by D-nnn*.

Format: `D-nnn · date · status (Proposed | Accepted | Superseded)`

---

### D-001 · 2026-11-09 · Proposed — Brand name "Tessera"
- **Decision:** Working brand is **Tessera** (a tile in a mosaic → modules composing a suite). Python package `tessera`, CLI `tessera`, env prefix `TESSERA_`.
- **Alternatives:** Cairn, Trellis, Mosaic, Keel.
- **Rationale:** Short, original, descriptive of modularity, not the name of a competing business suite.
- **Reversibility:** Easy before Phase 1 code; a rename script will be kept in `tools/rename_brand.py` through Phase 1.

### D-002 · 2026-11-09 · Proposed — FastAPI + custom metadata ORM (not Django)
- **Decision:** Backend is FastAPI on ASGI. Data access is a purpose-built metadata ORM ("the kernel") that generates SQL with SQLAlchemy Core over psycopg 3 and performs declarative schema synchronisation.
- **Alternatives:** Django + Django ORM; FastAPI + SQLAlchemy ORM (declarative classes).
- **Rationale:** The requirements (models extensible by other modules without forking, runtime field creation for Studio, views-as-data, domain-based record rules injected into every query, field-level visibility, polymorphic chatter, per-database registries for database-per-tenant) are a metadata-driven application framework. Django's static model classes and migration workflow fight each of these; the kernel is the main engineering investment either way, so building it on a thin, controllable SQL layer is cheaper than fighting a full-stack framework. FastAPI gives async I/O (realtime, AI streaming), Pydantic schemas and OpenAPI generation for free.
- **Consequences:** We own auth/session/CSRF plumbing (small, well-trodden), schema sync and migration tooling (required anyway for Studio), and admin UI (the generic client *is* the admin UI).
- **Reversibility:** Hard after Phase 1.1. Flagged for explicit approval.

### D-003 · 2026-11-09 · Proposed — Monorepo layout
- **Decision:** One repository: `backend/` (kernel + `modules/`), `gateway/` (AI gateway service), `frontend/` (React client), `deploy/`, `docs/`.
- **Rationale:** Atomic changes across kernel, modules and client; single CI; shared OpenAPI → TypeScript client generation.

### D-004 · 2026-11-09 · Proposed — Manifest format is TOML; views, menus, security and data are JSON
- **Decision:** `manifest.toml` per module; declarative artefacts are JSON files validated against published JSON Schemas; translations are `.po`.
- **Alternatives:** Python dict manifests; XML views; YAML everywhere.
- **Rationale:** TOML is unambiguous and non-executable for manifests. JSON is what the client consumes and what Studio writes back, so storing views as JSON avoids an XML↔JSON translation layer. YAML is reserved for AI prompt files where multi-line text dominates.

### D-005 · 2026-11-09 · Proposed — Domain DSL is nested JSON (`all` / `any` / `not` + leaf triples)
- **Decision:** `{"all": [...]}`, `{"any": [...]}`, `{"not": ...}`, leaf `[path, operator, value]`, variables `$uid`, `$company`, `$companies`, `$now`, `$today`, `$user.<field>`.
- **Alternatives:** Prefix (Polish) notation lists; SQL fragments; a text query language.
- **Rationale:** Explicit nesting is readable by humans, trivially round-trips through the client filter editor and AI natural-language-to-filter output, and compiles cleanly to SQL and to in-memory evaluation.

### D-006 · 2026-11-09 · Proposed — Model naming `module.model`, tables `module_model`
- **Decision:** Technical model names are dotted (`crm.lead`, `contacts.partner`, `core.user`); table names replace the dot with an underscore; m2m join tables are `<model_a>__<field>` deterministic names.
- **Rationale:** Namespacing by owning module makes dependency and uninstall analysis mechanical.

### D-007 · 2026-11-09 · Proposed — Migrations: declarative additive sync + versioned scripts with `down.py`
- **Decision:** Schema sync applies only additive/safe changes automatically; destructive or data migrations are explicit `pre.py`/`post.py` with a mandatory `down.py`. Uninstall keeps dropped columns renamed for 30 days unless `--drop-data`.
- **Rationale:** Reversible migrations are a stated non-functional requirement; Studio needs runtime additive sync anyway.

### D-008 · 2026-11-09 · Proposed — Background jobs: Celery + DB job ledger + DB-driven scheduler
- **Decision:** Celery with Redis broker executes work; every job also has a `core.job` row for visibility/retry/audit; `core.scheduled_job` rows drive a lock-protected scheduler process (no Celery Beat).
- **Rationale:** Celery is the requested default; the ledger gives admins a UI and durable retry state; a DB-driven scheduler lets modules and Studio define scheduled jobs as data.

### D-009 · 2026-11-09 · Proposed — Translatable fields stored as JSONB per language
- **Decision:** `translate=True` fields store `{"lang": value}` in a JSONB column; fallback chain user → company → `en_US`.
- **Alternatives:** Side translation table per field.
- **Rationale:** One column, indexable with expression indexes, simple reads and writes, no joins for list views.

### D-010 · 2026-11-09 · Proposed — All view types in the kernel contract from Phase 1; Gantt and map renderers ship in Phase 4
- **Decision:** The view schema, validation and server-side support for `gantt` and `map` exist in Phase 1; the client renderers are delivered with Project (Gantt) and Field Service (map), the first consumers.
- **Rationale:** Avoids building renderers with no consumer; keeps the metadata contract complete so Studio and modules can declare them early.

### D-011 · 2026-11-09 · Proposed — Phase 1 includes a minimal `product` module and `sale` quotations
- **Decision:** CRM Phase 1 requires "quotations created from opportunities" and a portal to view quotes, so Phase 1 ships `product` (basic products, no variants) and `sale` restricted to quotations (draft → sent → accepted/rejected/expired, PDF, portal acceptance). Phase 2 extends `sale` to orders, variants, pricelists, policies.
- **Rationale:** Keeps Phase 1 coherent end to end without pulling the whole Sales module forward.

### D-012 · 2026-11-09 · Proposed — Studio v1 in Phase 1
- **Decision:** Phase 1 delivers Studio's core (add fields, edit form/list/kanban layouts, create views, define automated actions, versioning and rollback). Dashboard drag-and-drop lands with the `dashboards` module (Phase 8); AI-assisted Studio lands in Phase 8 per the spec.
- **Rationale:** Studio exercises the kernel's metadata design while it is still cheap to adjust; it is also the admin's main customisation tool from day one.

### D-013 · 2026-11-09 · Proposed — SSO: OIDC in Phase 1, SAML in Phase 2
- **Rationale:** OIDC covers Google, Microsoft Entra, Okta, Keycloak; SAML needs `python3-saml`/xmlsec packaging and is less urgent.

### D-014 · 2026-11-09 · Proposed — AI gateway is a separate service; the AI runtime is in-process
- **Decision:** `gateway/` is a separately deployable FastAPI service holding provider credentials, routing, budgets, cache, redaction and the usage ledger. Prompts, tools, retrieval, proposals and evaluation live in the application (`tessera.ai` + `ai_core` module) because tools must execute with the user's `Environment`.
- **Alternatives:** Fully in-process library; fully external agent service that calls back into the app API.
- **Rationale:** Provider credentials and residency controls are isolated; the gateway scales independently and can be pinned per region; tools keep direct, permission-scoped ORM access without a second auth hop.

### D-015 · 2026-11-09 · Proposed — Embeddings and vector store
- **Decision:** `pgvector` in the main database (`ai_core.embedding`), HNSW index, cosine distance. Default embedding models: OpenAI `text-embedding-3-small` (hosted), Voyage for Anthropic-leaning deployments (Anthropic does not offer an embeddings API), and a self-hosted option via Ollama/TEI (`nomic-embed-text` or `bge-m3`). Embedding model is per company; re-indexing is a job.
- **Rationale:** Stays within the requested stack; record-rule filtering before ranking is a SQL join when the vectors live next to the records.

### D-016 · 2026-11-09 · Proposed — Default model tiers for the Anthropic adapter
- **Decision:** small/cheap tier `claude-haiku-5-5` (classification, extraction, routing, translation), mid tier `claude-sonnet-5-5` (summaries, drafts), large tier `claude-opus-5-5` (reasoning, proposals, complex extraction). Adapters for other providers declare equivalent tiers in their config; the routing table is per company and editable.
- **Rationale:** Matches the "small models for classification/extraction, larger models for drafting/reasoning" requirement at current list prices.

### D-017 · 2026-11-09 · Proposed — CI runs AI evaluations in replay mode; live evaluations nightly
- **Decision:** Provider responses for golden sets are recorded as cassettes; PR CI replays them deterministically and enforces thresholds; a nightly workflow with provider secrets runs live and files a report.
- **Rationale:** Deterministic, cheap, secret-free PR checks; live drift still detected daily.

### D-018 · 2026-11-09 · Proposed — Licence Apache-2.0
- **Decision:** Repository licence Apache-2.0 unless the owner specifies otherwise.
- **Rationale:** Permissive, patent grant, common for platform software. Easy to change before the first external release.

### D-019 · 2026-11-09 · Proposed — Soft delete + archive semantics
- **Decision:** Archive = `active` flag (reversible, default-filtered). Soft delete = `deleted_at` on `_soft_delete` models with 30-day trash and restore. Legally constrained records (posted entries, completed envelopes) refuse delete.

### D-020 · 2026-11-09 · Proposed — PDF rendering with WeasyPrint
- **Alternatives:** headless Chromium (Playwright) printing; ReportLab.
- **Rationale:** Pure-Python, CSS paged media, no browser in the worker image; Chromium remains an option for complex layouts.

### D-021 · 2026-11-09 · Proposed — Phase 1 is delivered in sub-phases 1.0–1.7
- **Decision:** Phase 1 is too large to land as one increment; the build plan splits it into 1.0 tooling, 1.1 kernel, 1.2 API + client foundation, 1.3 platform services, 1.4 apps, 1.5 AI platform, 1.6 AI features, 1.7 Studio v1. Each sub-phase has its own acceptance criteria and ends runnable and tested.

### D-022 · 2026-11-09 · Proposed — `sms` transport module ships in Phase 2b (with Sign), SMS marketing in Phase 6
- **Decision:** The `sms` module (provider adapters, outbound queue, delivery status, opt-out registry) is delivered with Phase 2b because Sign needs SMS one-time codes and SMS signing links. SMS *marketing* (campaigns, segmentation) stays in Phase 6 as an extension of `sms`.
- **Rationale:** Avoids Sign depending on a Phase 6 module; the transport layer is small and reusable.

### D-024 · 2026-11-10 · Accepted — The ORM is synchronous; FastAPI runs it in its threadpool
- **Decision:** The kernel ORM, module loader and security provider are synchronous (psycopg 3 through SQLAlchemy Core). API handlers are plain `def` endpoints (FastAPI dispatches them to a threadpool); `async` is reserved for WebSockets, SSE streaming and the AI gateway client.
- **Alternatives:** Fully async ORM.
- **Rationale:** A metadata ORM with recursive compute/related resolution, savepoints per DML statement and record-rule re-checks is far simpler to reason about synchronously; Postgres connection pooling makes the threadpool model perform well at the expected concurrency. Async where it pays (fan-out, streaming) keeps the benefits where they exist.

### D-025 · 2026-11-10 · Accepted — mypy: checked everywhere, strict on stable leaf packages
- **Decision:** `check_untyped_defs` across kernel and modules; `strict = true` only for stable, non-dynamic packages (`orm.domain`, `orm.cache`, `modules.graph`, `modules.manifest`, `exceptions`, `config`). `Model.__getattr__` is typed `Any` so `record.<field>` access type-checks. Strictness is tightened per package as APIs stabilise.
- **Rationale:** Fields are descriptors composed at runtime; demanding strict typing of every dynamic attribute would cost `# type: ignore` noise without catching real defects. Correctness of the dynamic parts is covered by the kernel test modules instead.

### D-023 · 2026-11-09 · Proposed — Public web surfaces before Phase 7 use `*_website` bridge modules
- **Decision:** Modules that need a public page before the `website` builder exists (recruitment job postings, event pages, eLearning courses, blog, forum, survey invitations) depend only on `portal` for their minimal public rendering in their own phase; `recruitment_website`, `events_website`, `elearning_website`, `blog_website`, `forum_website` bridge modules (auto-installed when `website` is present) move those pages into the website builder in Phase 7.
- **Rationale:** Preserves the layering rule (no dependency on a later phase) while still shipping usable public pages when each module lands.
