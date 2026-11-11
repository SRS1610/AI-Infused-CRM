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

### D-026 · 2026-11-10 · Accepted — REST paths are `/api/v1/models/{model}`
- **Decision:** Generated REST endpoints live under `/api/v1/models/` instead of directly under `/api/v1/` (ARCHITECTURE §4.16 and BUILD_PLAN 1.2-c wrote `/api/v1/<model>`).
- **Rationale:** Keeps model routes from shadowing (or being shadowed by) fixed routes such as `/auth`, `/web`, `/jsonrpc`, and makes API gateways/ingress rules simpler.

### D-027 · 2026-11-10 · Accepted — Auth hardening defaults
- **Decision:** 5 consecutive failed passwords lock the account for 15 minutes (counted in an independent transaction so the failed request's rollback does not erase it); 10 one-time TOTP recovery codes (stored as SHA-256) are issued on enrolment; login/logout with a stale session cookie and no CSRF token proceed anonymously instead of failing; anonymous callers without a `public` user get uid 0 (no rights), never the system user.

### D-028 · 2026-11-10 · Accepted — Realtime hub backends
- **Decision:** In-process fan-out by default (`TESSERA_REALTIME_BACKEND=memory`); Redis pub/sub (`redis`) for multi-process deployments. Events are published only after commit. Replies (`subscribed`, `pong`) share the per-connection queue so they are ordered after already-published events.

### D-029 · 2026-11-10 · Accepted — Client styling: CSS design tokens and hand-built accessible components (supersedes the Radix/Tailwind line of ARCHITECTURE §1.2)
- **Decision:** One stylesheet with CSS custom properties (light/dark tokens, AA contrast), native elements and small hand-written ARIA widgets (combobox, tabs, listbox, dialog). No Tailwind or Radix for now.
- **Rationale:** The generic renderer needs only a dozen primitives; owning them keeps the bundle at ~110 kB gzip, avoids class-name churn in metadata-driven markup, and keeps accessibility behaviour testable in our own suite. Radix can be introduced per component later without changing view definitions.

### D-030 · 2026-11-10 · Accepted — Typed API client is hand-written
- **Decision:** `frontend/src/kernel/api/{client,types}.ts` mirror the documented contract by hand instead of being generated from OpenAPI.
- **Rationale:** The generated OpenAPI describes generic endpoints (`/models/{model}`) whose payloads depend on runtime metadata, so generated types would be `Record<string, any>` anyway. Per-model OpenAPI schemas (and generated types for module-specific endpoints) are planned with the schemathesis contract suite.

### D-031 · 2026-11-10 · Accepted — npm (package-lock.json) instead of pnpm
- **Rationale:** npm ships with Node; one fewer tool for contributors and CI. Makefile, CI and the web image use `npm ci`.

### D-032 · 2026-11-10 · Accepted — Scheduler exactly-once through database row locks, not a Redis lock
- **Decision:** Each scheduler tick claims due `core.scheduled_job` rows with `SELECT … FOR UPDATE SKIP LOCKED`, advances `next_run` in the same transaction, and enqueues the job with idempotency key `sched:<id>:<due time>` (unique). BUILD_PLAN 1.3 mentioned a Redis lock.
- **Rationale:** The database already serialises the state that matters; a Redis lock adds a second source of truth with expiry races (a slow tick outliving its lock). Verified by a test running four concurrent ticks: one job per occurrence.

### D-033 · 2026-11-10 · Accepted — Job ledger in PostgreSQL; Celery is only the transport
- **Decision:** `core.job` + `core.job_attempt` hold state, attempts, errors, progress and results. Celery tasks carry only `(db_url, job_id)`; claiming uses a row lock, so redelivered messages (acks-late) never run a job twice. Jobs dispatch after commit; three backends: `celery`, `inline` (dev), `manual` (tests). A sweep recovers jobs whose worker died.
- **Rationale:** Jobs are visible and re-queueable from the UI, survive broker loss, and tests control execution deterministically.

### D-034 · 2026-11-10 · Accepted — Security cache keyed by a transactional stamp
- **Decision:** The access-rule cache in each process is tied to `core.security_stamp.version`, a random value rewritten in the same transaction as any change to groups, access rights, record rules, user memberships or group external ids; each transaction reads the stamp once and reloads the cache when it differs.
- **Rationale:** The previous in-process invalidation missed changes made by other workers and could serve rights granted inside a transaction that later rolled back (found by the global-search tests). A random value (not a counter) avoids a rolled-back change colliding with a later committed one.

### D-035 · 2026-11-10 · Accepted — Sandboxed expression language for automations
- **Decision:** `tessera.expr` evaluates Python-syntax expressions by walking a whitelisted AST (no statements, comprehensions, lambdas, method calls or non-field attributes; `_`-names rejected; step and size budgets). No `eval`, no RestrictedPython.
- **Rationale:** Automation and Studio formulas are written by administrators but stored as data and executed by the server; a closed evaluator is small enough to audit fully and its escape tests are part of the suite.

### D-036 · 2026-11-10 · Accepted — Thread access derives from the record
- **Decision:** `chatter.message`, `chatter.follower` and `chatter.activity` reference records generically (`model`, `res_id`). Ordinary users have no direct model access to messages/followers; the chatter API checks the record first and then reads the thread elevated. Portal users never see internal messages; recipients who cannot read a record are not notified about it. Activities are additionally readable by their assignee.
- **Rationale:** Record rules cannot express "readable if the parent record is readable" for generic references; one checked entry point is simpler and safer than per-model rules.

### D-037 · 2026-11-10 · Accepted — Import dry run = the real import inside a rolled-back savepoint
- **Decision:** The dry run executes conversion, create/write, constraints and automations exactly as the real run, inside a savepoint that is rolled back; rolling back a savepoint now also discards its after-commit callbacks and clears the ORM cache. Rows fail individually (per-row savepoints only for batches that fail).
- **Rationale:** A separate validation path would drift from the real one; this way the dry-run error list is exactly what the import will hit (1.3-i: 25 of 25 errors by row and column, nothing written).

### D-038 · 2026-11-10 · Accepted — Global search: one trigram index table, access re-checked after ranking
- **Decision:** `web.search_index` holds one row per record of models with `_global_search` (title, text, company), maintained by ORM hooks. A query ranks up to 200 candidates by trigram similarity, then re-runs a `search([("id", "in", …)])` per model as the caller so record rules apply before anything is returned.
- **Rationale:** One cross-model ranked query is fast with a GIN trigram index; re-checking with the ORM reuses the exact access logic. For RAG (Phase 1.5) the AI note requires filtering *before* ranking; that is handled in the retrieval layer, not here, because embeddings carry more sensitive content than titles.

### D-039 · 2026-11-10 · Accepted — Reports render in-process with WeasyPrint, templates in a Jinja2 sandbox
- **Decision:** `tessera.reports` renders Jinja2 templates (sandboxed, autoescaped, records exposed through read-only proxies, rendered as the requesting user) to HTML and then to PDF with WeasyPrint, synchronously for single documents and in the `reports` job queue for batches. Templates are versioned (`reports.template_version`) with publish and rollback.
- **Rationale:** No headless browser to operate; WeasyPrint supports paged media (running headers/footers, page counters). Rendering as the user means a template cannot print a field the user could not read.

### D-040 · 2026-11-10 · Accepted — Developer docs live in `docs/dev/`
- **Decision:** BUILD_PLAN paths `docs/developer/*` map to `docs/dev/*` (existing since 1.1); administrator docs are in `docs/admin/`.

### D-041 · 2026-11-10 · Accepted — Generic record APIs are for internal users only
- **Decision:** `/api/v1/models/*`, `/api/v1/jsonrpc` and `/api/v1/meta/*` require an internal (non-share) user. Portal users and anonymous token holders use purpose-built endpoints (`/portal/*`, `/sale/quotes/*`, `/chatter/*`) that expose only the fields and actions a customer needs; record rules still apply underneath.
- **Rationale:** Record rules limit *which rows* a customer reaches, but the generic API would still let them call any exposed method or read any field of those rows (1.4-n: "cannot call any non-portal method"). A narrow, reviewed customer surface is easier to keep safe than per-method portal flags.

### D-042 · 2026-11-10 · Accepted — Data files can extend another module's many2many links with `$add`
- **Decision:** `{"implied_ids": {"$add": ["sale.group_sale_manager"]}}` adds links without replacing them (a plain list still means "set"). Used so the administrator group implies the Sales and CRM administrator groups.

### D-043 · 2026-11-10 · Accepted — Calendar sync: one engine, provider adapters, change detection by content hash
- **Decision:** `calendar.sync` has one engine and thin adapters (CalDAV via WebDAV `sync-collection`, Google Calendar sync tokens, Microsoft Graph delta). Each linked event stores the remote ETag and a hash of the normalised iCalendar content; a remote change whose hash equals the stored one is treated as our own echo and ignored, and a conflict where both sides changed resolves to the most recent `last_modified`. Adapters are tested by one shared contract suite against an in-process Radicale server and fakes of the Google and Graph APIs.
- **Rationale:** Server timestamps have second granularity on some providers, so timestamp comparison alone misfires; hashes make echo suppression deterministic. A shared contract suite keeps the three adapters behaving identically.
- **Limits recorded:** modified single occurrences of a recurring series are not synced yet; Google OAuth token refresh and the Graph non-beta delta endpoint are left for the integration hardening in Phase 2.

### D-044 · 2026-11-10 · Accepted — Discuss presence is tracked per API process for now
- **Decision:** Online presence comes from the WebSocket hub of the process that holds the connection and is announced on the `presence` channel. With several API replicas a user connected to another replica shows as offline until the Redis-backed hub (Phase 2 scaling work) replaces the in-process one.
- **Rationale:** Single-process deployments are the Phase 1 target; message delivery itself already goes through the database and notification jobs, so only the green dot is affected.

### D-045 · 2026-11-10 · Accepted — Installing modules ensures PostgreSQL extensions first
- **Decision:** `install_modules` and `drop_everything` create the extensions modules rely on (`vector`, `pg_trgm`, `unaccent`, `btree_gin`, `pgcrypto`) before schema sync, so a brand-new database installs without a manual step; extensions the server lacks are reported, not fatal. The installer test (1.4-a) installs `crm` with its dependencies on an empty database and asserts no schema drift.

### D-046 · 2026-11-10 · Accepted — Gateway adapters: official SDKs for Anthropic and OpenAI, REST for Gemini and Voyage; breakers per process
- **Decision:** The Anthropic adapter uses the `anthropic` SDK (Messages API, beta endpoint so Opus 5.5 / Sonnet 5.5 get the provider's server-side refusal fallback); OpenAI, Azure OpenAI and OpenAI-compatible servers use the `openai` SDK; Gemini and Voyage are called over their REST APIs with `httpx2`. Circuit breakers and the response cache's memory variant live in each gateway process. Contract-test fixtures are hand-written in each provider's documented wire format until the nightly live run records real ones.
- **Rationale:** SDKs for the two providers with the richest features (strict tools, structured outputs, streaming helpers); REST keeps the dependency set small where an SDK adds little. Per-process breakers are enough for Phase 1 deployments (one gateway replica).

### D-047 · 2026-11-10 · Accepted — One embedding size (1024) for the whole platform
- **Decision:** `ai_core.embedding.vector` is `vector(1024)` with an HNSW cosine index. Adapters request 1024 dimensions where the provider supports it (OpenAI `dimensions`, Voyage `output_dimension`, Gemini `outputDimensionality`) and otherwise zero-pad or truncate-and-renormalise.
- **Rationale:** HNSW needs a fixed size; one size keeps switching embedding providers a re-index job rather than a schema change.

### D-048 · 2026-11-10 · Accepted — Rule-based PII redaction plus names known from the context (no NER model in Phase 1)
- **Decision:** The gateway redacts e-mail addresses, phone numbers, IBANs, Luhn-valid card numbers, US SSNs, honorific-prefixed names and the names the runtime passes in (people and companies linked to the record, `pii=True` text fields, the user). Placeholders are stable per request and restored in the answer and in streamed text.
- **Rationale:** Settles the open item in the AI note §8. Known names catch the people that matter in business records with no model to run; free-text names typed into descriptions are a documented gap (docs/ai/governance.md).

### D-049 · 2026-11-10 · Accepted — In-process gateway mode
- **Decision:** `TESSERA_AI_GATEWAY_URL=inprocess` runs the gateway library inside the API process, reading its configuration through the ORM and writing the ledger through the caller's cursor. Tests, the end-to-end suite and single-process installations use it; the backend depends on `tessera-gateway` only in its dev group.
- **Rationale:** Tests see uncommitted configuration and run without a second service, while exercising the same routing, budget, redaction and validation code as production.

### D-050 · 2026-11-10 · Accepted — Evaluation cassettes keyed by prompt version, case and call index
- **Decision:** Replay cassettes (`ai/cassettes/<feature>.jsonl`) store the gateway response for each (prompt version, eval case, call index). A changed prompt has a new version and therefore needs re-recording. Cassettes may refer to setup records symbolically (`$id:ref`, `{{src:ref}}`). Until provider keys are available, cassettes are hand-authored, so replay measures the platform's handling (validation, citations, access control, proposals), not model quality.
- **Rationale:** Deterministic CI with no secrets (D-017) and keys that survive the changing record ids of fresh fixtures.

### D-051 · 2026-11-10 · Accepted — Tools run in rolled-back savepoints; proposals are signed and owned
- **Decision:** Every tool call executes in a savepoint that is always rolled back; `writes=True` tools can only queue proposals. Proposals store an HMAC of their payload, may only be decided by the user who asked, expire after 7 days and are applied as that user in a fresh access check. Accepting a message proposal posts it as the user, linked to the run (`chatter.message.ai_run_id`); the AI-drafted label disappears once a person edits or accepts.
- **Rationale:** Makes "0 records mutated without a proposal" hold by construction, whatever the model does (prompt-injection corpus, 1.5-g).

### D-052 · 2026-11-10 · Accepted — Uninstall moves indexes and join tables aside with the tables
- **Decision:** Uninstalling a module without `--drop-data` renames its tables *and* their indexes (to short hashed names) and treats its many2many join tables as owned. Found while uninstalling and reinstalling `ai_core`: the old indexes blocked the reinstall and the join table showed as drift.
- **Rationale:** U-2 requires a clean `tessera db check` after uninstalling a leaf module, and reinstalling must work.

### D-053 · 2026-11-10 · Accepted — Studio fields are per-database dynamic fragments; a registry stamp tells other processes
- **Decision:** Studio fields are rows of `studio.field`. A `before_build` registry hook turns the committed rows of the registry's own database into model fragments (module `studio`), so two databases served by one process keep separate customisations. Adding a field rebuilds the in-process registry from the transaction's rows, runs the additive schema sync and the metadata sync, and rewrites `core.registry_stamp`; every API request, job and `environment_for` compares the stamp and rebuilds when it changed. If the transaction rolls back, a new `Cursor.on_rollback` callback rebuilds the registry from the committed state.
- **Rationale:** "Without restart" (1.7-a) across workers, using the kernel's existing composition of fragments; no generated Python files on disk.
- **Limits:** a rebuild happens under the registry lock; requests already running keep their model classes (the rebuild is published in one step, D-060). Studio layout editing covers form field order, visibility and read-only; other view types are edited with view operations through the API.

### D-054 · 2026-11-10 · Accepted — Studio changes are ordinary view extensions, automations and workflows, logged in `studio.change`
- **Decision:** Layout changes are extension views (`web.view` with `inherit_id`, `studio=True`, sequence 1000) holding inheritance operations; automations and workflows are ordinary `workflow.*` records. Each change records before/after metadata and the record it created; rollback deletes that record (or, for a field, deactivates it, removes it from Studio view changes that use it, and renames its column). Export writes the applied changes as data files of a `studio_customizations_<company>` module.
- **Rationale:** No second customisation mechanism next to modules: what Studio produces is what a developer would have written, so it exports and installs like any module.

### D-055 · 2026-11-10 · Accepted — `tessera seed demo`
- **Decision:** `tessera seed demo --phase 1` installs the phase's modules with demo data and runs every module's `seed_demo` hook (retrieval index, trained models, sample runs).
- **Rationale:** The phase definition (§1.2) needs one command that prepares the demo scenario.

### D-056 · 2026-11-10 · Accepted — Predictive CRM features are classical models trained per company
- **Decision:** Lead scoring is a per-company logistic regression trained nightly on won/lost history, stored as JSON (version, hash, training metadata; no pickle) and scored in-process; companies without enough history fall back to the rule scoring. Inputs come from an allow-list that excludes names, places, language, job titles and every protected attribute or proxy (tested). Deal risk is weighted signals with a phrase list for sentiment; routing combines assignment rules, each member's record on similar deals and current load, and is always a proposal unless a rule explicitly allows auto-apply (off by default). The forecast is a Monte-Carlo simulation over stage win rates and slippage; an optional LLM narrative never changes the figures.
- **Rationale:** AI architecture note §4.7: numbers come from models that can be explained factor by factor; LLMs only narrate.

### D-057 · 2026-11-10 · Accepted — Contact enrichment goes through approved adapters only
- **Decision:** Enrichment sources implement an adapter interface and are approved per company (`contacts.enrichment_source`; a company row overrides the global one). A suggestion is kept only if an approved adapter returned it, always carries its source, and values from different outside companies are never mixed in one proposal. Phase 1 ships an offline fixture adapter; real adapters plug into `modules/contacts/enrichment.py`.
- **Rationale:** Source attribution must be 100 % (1.6 table), and no data provider is contracted yet.

### D-058 · 2026-11-10 · Accepted — How Phase 1 AI features are measured
- **Decision:** Deterministic and statistical features (scoring, risk, routing, forecast, suggested times, duplicates, tag/enrichment accuracy, to-do ranking, translation chrF, domain equivalence, price grounding) are measured for real by the production code on generated data with a known signal, seeded separately from training. LLM features are measured by replaying hand-authored answers and judge scores (D-050), which tests validation, citations, access control, proposals and injection handling, not model quality. Live scores need provider keys and the nightly run.
- **Rationale:** States plainly what the green thresholds mean until a live provider is configured.

### D-059 · 2026-11-10 · Accepted — API rate limits are per credential, with a higher ceiling per address
- **Decision:** Authenticated API requests are counted per credential (API key or browser session, hashed) against `api_rate_limit_per_minute` (600), and per client address against `api_ip_rate_limit_per_minute` (6000) so rotating credentials cannot bypass it; requests without a credential share a per-address budget of 600. Every 429 carries `Retry-After`; the web client retries queries after that delay (up to three times) instead of showing an error.
- **Rationale:** Keyed by address alone, an office behind one NAT shared a single budget of 600 requests a minute; found when the end-to-end suite (one address, many sessions) was throttled mid-test.

### D-060 · 2026-11-10 · Accepted — Registry rebuilds are staged and published in one step
- **Decision:** `Registry.build()` composes the models on a shallow staging copy and then replaces the live registry's models, tables, metadata and triggers in a single `__dict__.update`. A savepoint that is released hands its rollback callbacks (`Cursor.on_rollback`) to the enclosing level, so a later sibling savepoint's rollback no longer runs them. With the in-process gateway, the API imports the gateway and its provider SDKs at startup.
- **Rationale:** A Studio change rebuilds the registry while other requests run; clearing the dictionaries in place let a concurrent request see a half-built registry ("Model 'core.group' has no table"), and a sibling savepoint's rollback could reset the registry to the committed state while keeping the new stamp, hiding a new field until the next change. The SDK imports (~1.5 s) otherwise landed on the first user's AI request and broke the 1.5 s first-token budget after every restart.

### D-061 · 2026-11-10 · Accepted — Server translations: per-module gettext catalogues, applied at read time and on install
- **Decision:** Each module ships `i18n/<module>.pot` and `i18n/<lang>.po` (found by convention, read with Babel); lookups merge the installed modules' catalogues, the field's own module first. Labels, help, selection labels, model descriptions and view text are translated when read (not copied into `core.model_field` or views); `translate=True` records (menus, actions, stages, groups…) receive each catalogue language on install, upgrade and `tessera i18n load` — records flagged noupdate only gain missing languages unless `--overwrite`. `_("… {x}", x=…)` returns a message that keeps its source and parameters, so error handlers and notifications render it again in the caller's or recipient's language; outside a request it stays English. Kernel messages belong to the `core` catalogue. A test fails when a Phase 1 catalogue drops below 95 % or breaks a placeholder.
- **Rationale:** Implements D-004 (`.po` files) without a data migration and without touching Studio's view records; administrators' edits to translated records are kept.
- **Limit:** D-009's company step is not implemented for catalogues (`core.company` has no language); the fallback is user language → same base language → English.

### D-062 · 2026-11-10 · Accepted — Studio v1 completion: conditions on fields, edit and workflow changes, safe rollback
- **Decision:** `required_if` / `readonly_if` are domains stored on `studio.field`; the client evaluates both and the server enforces `required_if` (read-only stays a UI rule). Editing a Studio field is its own change kind (`edit_field`) with before/after options. List columns, filters and group-bys are appended with a Studio-only `append_to` operation that the server translates into plain inheritance operations. A new view becomes the default by priority. A workflow is replaced, not edited in place (the old one archived and restored on rollback), and states on a link field carry their record's external id so exports install on other databases. Rolling back a change that a later change relies on is refused.
- **Rationale:** What Studio stores stays what a developer would write (D-054), every change rolls back on its own, and a stored customisation always resolves.

### D-063 · 2026-11-10 · Accepted — Evaluations run in a corpus sandbox
- **Decision:** During `tessera ai eval run`, searches of business models (and the features' own ranking queries through `sql_filter()`) see only the records created by the run and the `$xml:` records its cases name; each feature's run is rolled back. Lost opportunities stay in the retrieval index (model-level `_ai_index_archived` domain) and are returned only to searches that include archived records, such as objection handling.
- **Rationale:** Scores must not depend on the target database's business data: on a demo database with overlapping deals, citation validity fell to 0.0 without the sandbox and stays 1.0 with it, and nothing is written.

### D-064 · 2026-11-10 · Accepted — Calendar OAuth refresh, cross-process presence, website enrichment
- **Decision:** Calendar accounts store refresh and access tokens with their expiry (encrypted, administrators only), refresh them before expiry or on a 401, and only a refusal from the provider or a missing OAuth client moves an account to "Needs reconnection" (its owner is notified once; outages are retried). OAuth clients come from the company setting `calendar.oauth_clients` or `TESSERA_*_OAUTH_*`. With the Redis realtime backend, presence is one sorted set of connections per database with expiry scores, refreshed by a heartbeat, so a crashed process cleans up by itself. The keyless "Company website" enrichment adapter reads a company's public home page (https only, public addresses only, re-checked redirects, size and time limits) and connects to the checked address directly, ignoring proxy settings.
- **Rationale:** Closes the Phase 1 gaps without new dependencies; every enrichment value keeps its source (D-057).

### D-065 · 2026-11-10 · Accepted — Out-of-process module changes reload registries; database sessions keep their settings
- **Decision:** Module install, upgrade and uninstall rewrite `core.registry_stamp` like Studio does (D-053); a process whose registry was loaded for another stamp loads a fresh registry when its installed modules differ, or rebuilds it when only Studio fields changed. The engine's connect hook commits its session settings (statement timeout, UTC, `jit = off`); `TESSERA_DB_JIT=true` turns JIT back on.
- **Rationale:** Found by the Docker Compose check (U-1): the API started on an empty database kept answering "Unknown model" after `tessera seed demo` installed the modules from another process. Found by the 100k-row check (U-6): the pool's rollback undid the connect hook's SETs, so sessions ran without a statement timeout, and PostgreSQL's JIT added ~160 ms to a 20 ms list count.

### D-066 · 2026-11-10 · Accepted — Development stack object store and image sources
- **Decision:** The Compose stack uses SeaweedFS's S3 gateway instead of MinIO (whose images are no longer published on Docker Hub); the S3 backend creates its bucket on first write. Images pin uv (`UV_IMAGE`, overridable for mirrors) and a `.dockerignore` keeps the build context to the sources. Production deployments point `TESSERA_S3_*` at their own S3 service.
- **Rationale:** `make up` failed on a fresh machine; the build context was 842 MB.

### D-067 · 2026-11-10 · Accepted — Security hardening from the Phase 1 ASVS L2 review
- **Decision:** The request context the client may send is an allow-list (`lang`, `tz`, `active_test`, `include_deleted`, `default_*`, `search_default_*`). Models holding per-user records (API keys, push subscriptions, imports, saved filters, AI runs, calendar accounts…) re-check record rules after a write (`_rule_check_after_write`), and on every model a record can only be moved into one of the caller's companies. Attachments follow the access of their record on every path; files are shown inline only for safe types (images except SVG, PDF, text, CSV, audio, video). Linking or creating an account through single sign-on requires a verified e-mail, and only asymmetric signing algorithms are accepted. Non-administrator Studio designers cannot automate `core.*` models. The PDF renderer loads `data:` resources only. `X-Forwarded-For` is trusted only from `TESSERA_TRUSTED_PROXIES`; outbound calls to private addresses are refused in production unless `TESSERA_OUTBOUND_ALLOW_PRIVATE`; production refuses development keys and a `*` CORS origin.
- **Rationale:** Fixes the seven high-severity findings of `docs/security/asvs-phase-1.md` (U-9). Open medium and low findings are scheduled there.

### D-068 · 2026-11-10 · Accepted — API metrics endpoint
- **Decision:** The API serves `GET /metrics` (Prometheus): requests labelled by route template and status class, background job attempts and run time per job and outcome, AI feature runs per feature and outcome with tokens, cost and provider time, and the in-process gateway's metrics. Open in development; in production only with `TESSERA_METRICS_TOKEN`.
- **Rationale:** U-10 asks for metrics of every job and AI feature; only the gateway service had them, so jobs and in-process AI were invisible. Route templates (not raw paths) keep label cardinality bounded; metrics reveal usage, so production needs a token.

### D-069 · 2026-11-10 · Accepted — Phase 2 is delivered in sub-phases 2.0–2.7; module names follow the build plan
- **Decision:** Phase 2 lands as 2.0 plan and data model, 2.1 accounting core, 2.2 product and sales, 2.3 bank / payments / dunning / e-invoicing, 2.4 analytic / budgets / assets / reports / tax return, 2.5 subscriptions / rentals / commissions, 2.6 AI features, 2.7 SAML, demo and acceptance (BUILD_PLAN §3.1). Module names are the build plan's (`account_tax`, `account_reports`, `account_dunning`, `account_einvoice`); the dependency map's `account_tax_report` and `einvoice` are renamed to match. SAML ships as its own `auth_saml` module. Modules whose AI feature the plan does not list ship the dependency map's: `product` description drafts, `account_analytic` allocation suggestions, `account_einvoice` mapping-error explanations, `payment` failed-payment summaries.
- **Alternatives:** One increment for the whole phase; the dependency map's names.
- **Rationale:** Keeps the branch runnable at each boundary and puts the ledger (everything else posts through it) first. Every module ships an AI feature (§1.2-6).

### D-070 · 2026-11-10 · Accepted — Units of measure stay in `product`
- **Decision:** `product.uom` (Phase 1) gains reference / bigger / smaller types, rounding and conversion; there is no separate `uom` module in Phase 2.
- **Alternatives:** Move units to a new `uom` module (model rename and data migration on every Phase 1 database).
- **Rationale:** The dependency map already counts units as part of `product`; nothing in Phase 2 needs units without products. Revisit if Phase 3 needs units without `product`.

### D-071 · 2026-11-10 · Accepted — Product variants are child products of a template product
- **Decision:** A product with attribute lines is a template (not sold itself); generating variants creates one `product.product` per combination with `template_id`, `attribute_value_ids` and `price_extra`. Shared fields are copied from the template and kept in sync; they are read-only on variants.
- **Alternatives:** Separate `product.template` model with delegation inheritance (the ORM has none; every Phase 1 reference to `product.product` and every product field would move).
- **Rationale:** Every document keeps pointing at `product.product`, Phase 1 products need no migration, and a variant is a real product (its own code, barcode, price, stock in Phase 3).

### D-072 · 2026-11-10 · Accepted — Partner accounting settings are optional overrides with company fallback
- **Decision:** `contacts.partner` gains receivable / payable accounts, payment terms and fiscal position as plain fields. A document uses the partner's value only when it belongs to the document's company; otherwise the company default applies.
- **Alternatives:** Company-dependent property values in a side table.
- **Rationale:** Partners are shared between companies (Phase 1); the fallback makes a shared partner usable everywhere without a property engine. Revisit if customers need per-company overrides on shared partners.

### D-073 · 2026-11-10 · Accepted — Ledger invariants are enforced by the ORM and by PostgreSQL
- **Decision:** Journal items carry `debit`, `credit` and `balance` with `CHECK` constraints; a deferred constraint trigger refuses any commit that leaves a posted entry unbalanced; posted entries are read-only except for reconciliation and narrative fields and can never be deleted. Numbers are allocated at posting from a gapless sequence per journal, fiscal year and refund flag (`INV/2026/00001`); cancelled entries keep theirs. Lock dates (fiscal year, period, tax) are checked on post, reset and cancel. Sales and purchase journals chain a SHA-256 hash over posted entries; `tessera account integrity` verifies it.
- **Alternatives:** ORM checks only; numbering at creation.
- **Rationale:** Build plan risk 6 and the P2 security track (accounting lock integrity): a bug or a direct SQL write cannot silently unbalance the ledger, and numbering at posting is what tax authorities expect (no gaps from abandoned drafts).

### D-074 · 2026-11-10 · Accepted — `sale.quotation` remains the sales document
- **Decision:** A quotation becomes a sales order in place when it is accepted or confirmed (state `accepted`, labelled *Sales order*). Line taxes become `account.tax` records; the `sale` 2.0.0 migration maps each distinct Phase 1 `tax_percent` to a sale tax of that rate per company and drops the column.
- **Alternatives:** A new `sale.order` model with a data migration from quotations.
- **Rationale:** D-011 planned the in-place extension; every Phase 1 reference (CRM, portal, PDF, AI features) keeps working.

### D-075 · 2026-11-10 · Accepted — Product variants and pricelist rules (product 2.0)
- **Decision:** Shared variant fields are `_variant_shared_fields` (name, type, category, unit, sales description, prices, currency, company, sale flag, image; `account` adds taxes, income / expense accounts and the purchase flag). A variant's sales price is the template's plus `price_extra`. Writing a different value on a variant is refused; resending the same value is accepted. Generation never deletes: stale variants are archived, returning combinations reactivated. Documents choose line products with the stored `is_sellable` (can be sold and not a template). A fixed price on a template rule still adds the variant's extra; margins apply when set. `_for_partner` uses the partner's pricelist when it is shared or in the company (D-072), then a regional list, then the company's first list without countries. Catalogue menus live under a *Catalogue* root until `sale` 2.0 places them.
- **Alternatives:** Refuse any shared-field write on variants; hide templates from searches.
- **Rationale:** Phase 1 references and searches keep working (D-071) and clients that resend unchanged values are tolerated.

### D-076 · 2026-11-10 · Accepted — SAML SSO: signxml, service-provider initiated only, explicit account linking
- **Decision:** `auth_saml` implements SAML 2.0 Web Browser SSO as a service provider (HTTP-Redirect requests, optionally signed; HTTP-POST responses). Signatures are verified with `signxml` against the configured IdP certificates only (SHA-1, HMAC and DSA refused); a response must contain exactly one assertion and only the element the verifier returns as signed is read. Encrypted assertions and IdP-initiated responses are refused. Request IDs are single use and bound to the browser by an HttpOnly cookie; assertion IDs go into a replay cache. Accounts match by (provider, NameID) link first; e-mail links an existing account only when the provider is marked `trust_email`, and accounts are created on first login only with `allow_signup` (both off by default). Sessions are created as for OIDC. Modules can register login endpoints (`register_login_path`, `exempt_login_path`).
- **Alternatives:** `python3-saml` / `xmlsec` (native library); encrypted assertions and IdP-initiated login; linking by e-mail by default.
- **Rationale:** No native dependency, defences against signature wrapping, XXE and replay, and the same account-linking rule as OIDC (D-067).

### D-077 · 2026-11-10 · Accepted — Accounting behaviours chosen in sub-phase 2.1
- **Decision:** (1) Closing a fiscal year locks it; there is no closing entry: reports compute the current year's earnings and carry earlier results to retained earnings. (2) Hash-locked entries can be cancelled (state change, audited) but never reset to draft; their content stays covered by the chain. (3) Payments are booked straight on the bank / cash account unless the journal has *Payments wait for bank matching*, in which case they use the outstanding receipts / payments accounts and invoices show *In payment* until a statement line matches them. (4) A payment in another currency than the invoice books its receivable / payable item in the invoice currency so the realised exchange difference appears at reconciliation (2-c). (5) Unrealised differences are revalued per account, currency and partner at month end and reversed the next day; each revaluation item is matched with its reversal so open items and aged balances do not move. (6) Chart records get external ids `account.<company id>_<template id>`; a company created later gets the chart of its country. (7) Form buttons may declare `params` (and `confirm`): the client asks for them in a dialog and passes them to the method.
- **Alternatives:** Closing entries; refusing cancellation of hashed entries; always using outstanding accounts.
- **Rationale:** Simplest model that meets 2-a to 2-c and keeps invoices *Paid* when bank statements are not used.

### D-078 · 2026-11-10 · Accepted — Analytic sign and distribution format
- **Decision:** `analytic_distribution` is JSON `{"<analytic account id>": percent}`, at most 100 % per plan. Analytic item amount = −balance × % in company currency (revenue positive, costs negative). The distribution is the only analytic data writable on posted items; changing it regenerates the items. Distribution models apply only to lines created without a distribution, most specific first, filling plan by plan.
- **Alternatives:** One analytic account per line; analytic sign = balance.
- **Rationale:** Several plans (cost centre, project, department) at once; positive revenue reads naturally in analytic reports.

### D-079 · 2026-11-10 · Accepted — Budget amounts
- **Decision:** Planned and actual amounts are positive for the line's nature (expense lines count costs, revenue lines income); variance = actual − planned. Actuals are stored, refreshed when items are posted or changed and nightly. `variance_report()` holds the only numbers the 2.6 explanations may use.
- **Rationale:** Groupable in list / pivot views; one source of numbers for AI explanations.

### D-080 · 2026-11-10 · Accepted — Depreciation engine
- **Decision:** Life is measured in periods (`none` / `constant_periods`, 30/360 day count) or days (`daily_computation`); a pause does not use up life (the end moves). Straight line uses cumulative rounding; declining = residual × factor × months / 12, the last entry taking what is left; declining-then-linear takes the larger of the declining amount and residual over remaining life. Rates apply to book value minus salvage.
- **Rationale:** Matches the 2-g golden table and common statutory practice.

### D-081 · 2026-11-10 · Accepted — Asset changes
- **Decision:** A value decrease is an impairment entry on the depreciation expense account; an increase creates a gross-increase child asset over the parent's remaining life (counterpart *Other income* unless given). A cash sale debits outstanding receipts; a sale on an invoice line uses that line's account. Posted depreciation entries are changed through *Modify*, never reset. A due entry in a locked period posts on the first open day.
- **Rationale:** Keeps posted history immutable (D-073) while allowing revaluation and disposal.

### D-082 · 2026-11-10 · Accepted — Financial reports are computed live from journal items with data-driven definitions
- **Decision:** `account_reports` aggregates posted (optionally draft) journal items in SQL per company, period and account. Reports are `account.report` records whose lines use expressions `type()`, `code()`, `tag()`, `grid()`, `cash()`, `line()` and children, with per-term date scopes (period, as of, opening, fiscal year to date, before the fiscal year); `variant_of` + `chart_template` give per-chart versions. The balance sheet computes current-year earnings and retained earnings (no closing entry, D-077); the trial balance resets P&L accounts at the fiscal year start. Cash flow uses the direct method (counterparts of bank / cash entries by account type). Aged balances use residuals as of the date from the partial matches' dates. Consolidation converts each company with the shared rate table: balance sheet at closing rate, P&L and flows at the time-weighted average rate, equity at closing rate, differences on translation lines; no intercompany elimination yet. `ReportParams` + `run_report` is the only interface the 2.6 AI feature uses.
- **Alternatives:** Stored balances or materialised views; indirect cash flow; closing entries.
- **Rationale:** Meets 2-a and 2-l (< 450 ms p95 on 100k items) without extra tables or invalidation; countries add reports as data.

### D-083 · 2026-11-10 · Accepted — Tax return grids, tie and closing
- **Decision:** A grid amount = Σ balance × document direction (−1 on customer invoices / vendor credit notes, +1 on vendor bills / customer credit notes) × (−1 for `-NN` tags), so credit notes reduce grids. Every tax report defines `TAX_DUE`, `TAX_DEDUCTIBLE` and `NET`; `NET` must equal minus the movement of the tax accounts. Closing (advisers) posts one entry per period clearing the tax accounts per tax group into the group's payable / receivable account, sets `tax_lock_date`, stores a snapshot on `account.tax_return`, and is refused when the report does not tie unless forced. Pre-filing checks are deterministic with stable codes.
- **Alternatives:** Absolute amounts per tag; a single tax payable account; no tie guard.
- **Rationale:** Makes 2-e verifiable and follows the chart templates' tax groups.

### D-084 · 2026-11-10 · Accepted — Sales orders 2.0 behaviours
- **Decision:** (1) Line prices follow the pricelist until a price is typed (`price_manual`); clearing it returns to the pricelist. (2) `qty_invoiced` counts linked customer invoice lines, draft or posted, not cancelled; credit notes subtract; `qty_to_invoice` = ordered (or delivered) − returned − invoiced, negative meaning a credit is due. (3) Down payments are a percentage of the total or a tax-included amount, recorded as quantity-0 order lines per tax set, invoiced on the customer deposits account and deducted on the next regular invoice. (4) Promotions are applied explicitly, idempotent, keep the taxes of the discounted lines, never take an order below zero and are frozen once invoiced. (5) An order locks when the invoice that completes it is posted; locked orders still accept delivered / returned quantities and credit notes. (6) Invoicing from orders requires *Accounting / Billing*. (7) The 2.0.0 migration maps each non-zero Phase 1 `tax_percent` to a reused or created sales tax per company (0 % → no tax) and drops the line column; `product.tax_percent` stays as a hidden placeholder. (8) `portal` no longer depends on `sale` (dependency cycle through `account`); `tessera db migrate` installs the dependencies a new module version requires before upgrading.
- **Alternatives:** Prices always recomputed; automatic promotions on every save; a separate sales order model (D-074).
- **Rationale:** Predictable prices, idempotent promotions, and a Phase 1 database upgrades in one command (U-3).

### D-085 · 2026-11-10 · Accepted — Bank matching rebuilds the transaction's entry through the ledger
- **Decision:** A posted bank transaction is booked bank ↔ suspense. Matching resets its entry to draft (lock dates apply), replaces the suspense item with one counterpart per matched item (on that item's account, partner and currency) plus write-offs, keeps any unexplained rest on suspense, posts again keeping its number, and reconciles each counterpart. Undo restores the suspense item. *Payments wait for bank matching* stays a per-journal setting, off by default (D-077); the demo turns it on and the guide recommends it when statements are imported. Bank journals used for statements cannot be hash-locked.
- **Alternatives:** A separate matching entry; editing the posted entry in place.
- **Rationale:** Posted entries stay read-only (D-073) and lock dates apply; invoices stay *Paid* for companies that do not import statements.

### D-086 · 2026-11-10 · Accepted — Online payments: adapters over REST, events recorded once
- **Decision:** Providers implement checkout, webhook, token charge, refund and status. Stripe uses `httpx` (no SDK) with the `Stripe-Signature` HMAC-SHA256 check (5-minute tolerance, constant-time compare). Every notification is stored under `UNIQUE (provider_id, event_id)` before it is applied, so a replay is a no-op (2-j); transactions never leave a final state. A confirmed transaction books an `account.payment` through `register_payments`; money for an invoice already paid stays as an unmatched customer credit. Keys, webhook secrets and saved-card references are encrypted and readable by advisers only.
- **Rationale:** No SDK dependency; idempotent by construction.

### D-087 · 2026-11-10 · Accepted — E-invoices are validated with the official artefacts
- **Decision:** The OASIS UBL 2.1 schemas (unmodified, plus wrapper schemas), the CEN EN16931 rules and the OpenPeppol BIS 3 rules are vendored with their licences and run with Saxon-HE (`saxonche`). A document with a fatal finding is not sent; findings are stored as structured records on `account.edi_document`. The mock access point stores its "network" in a table so send, status and receive work across processes.
- **Alternatives:** Re-implementing the rules in Python.
- **Rationale:** 2-h asks for the official schematron; structured findings feed the 2.6 explanations.

### D-088 · 2026-11-10 · Accepted — Payment reminders
- **Decision:** A customer gets the highest level reached by their oldest overdue, undisputed invoice, excluding invoices *in payment*, excluded customers and balances under the company minimum. A level (or a higher one) is not sent again within the level's delay. Amounts and invoice tables always come from the ledger; message text goes through a replaceable renderer so the 2.6 feature can propose personalised text for review.
- **Rationale:** Predictable, never over-reminding, numbers never from a model.

### D-089 · 2026-11-11 · Accepted — Rentals: priced by covering duration, reserved per unit with a database exclusion constraint
- **Decision:** A rental's duration is counted in started hours on the salesperson's wall clock (daylight-saving changes never add or remove an hour). Its price is the cheapest combination of pricing rules (hour, day, week, month = 30 days) covering the duration, found by dynamic programming (ties: fewer blocks); rules of the order's pricelist take precedence, otherwise the sales price is a price per day. Confirming reserves numbered units for the half-open period [start, end); PostgreSQL refuses overlapping reservations of a unit with an `EXCLUDE USING gist` constraint (`btree_gist`, created by the installer), and a per-product advisory lock serialises confirmations so the second gets a clear message. Rentals are invoiced by ordered quantity; the deposit is a separate line on customer deposits, credited after return; late fees are added at return.
- **Alternatives:** Capacity counting without units; serial numbers (Phase 3).
- **Rationale:** Double-booking is impossible whatever writes the data, including concurrent transactions (2-f).

### D-090 · 2026-11-11 · Accepted — Commission statements
- **Decision:** The salesperson is the invoice's `invoice_user_id` (a credit note made from an invoice keeps the original's). On the invoiced basis lines count on their accounting date, so credit notes reduce the period they are posted in; on the paid basis invoices count when fully paid. The most specific rule wins (product, nearest category, all); raw commission = base × rate + quantity × fixed amount. The accelerator band (highest lower bound at or below the achievement, inclusive) applies to the whole period; each line is rounded half-up to the cent. Drafts recompute idempotently, confirmed statements are frozen; payout is a vendor bill per salesperson and the statement is *Paid* when the bill is. Salespeople see their own statements. `sale_commission` depends on `crm` for sales teams.
- **Rationale:** Reproducible by hand (2-k); the AI explanation cites only statement numbers.

### D-091 · 2026-11-11 · Accepted — Subscriptions are sales orders billed on an anchored grid
- **Decision:** (1) A subscription is a `sale.quotation` with a plan and products flagged `recurring_invoice`; its recurring lines are never invoiced by regular invoicing and the order never locks. (2) Period *k* starts on anchor + *k* periods, computed from the anchor each time (the 31st falls back to month end). Each period is invoiced once, dated on its first day, with a unique (subscription, period start) key; cancelling the invoice frees the period. (3) Prorations count calendar days, both ends included: `(period end − change date + 1) / period length`, rounded per line to the cent; mid-period changes are netted into one invoice or one credit note; a fixed term ending mid-period invoices only the covered days. (4) Closing *now* credits the unused days on the current lines; *at period end* credits nothing; payment-failure and paused closures give no credit. (5) `closed` means replaced by an accepted renewal (starting the day after the end date); every other ending is `churned`, the churn flag. (6) Every MRR movement is logged (new, upsell, downsell, churn, reactivation, renewal) in order and company currency; KPIs and churn features come from that log. Pausing does not change MRR.
- **Alternatives:** Extending the same order in place on renewal; chaining period dates; crediting pauses.
- **Rationale:** Deterministic, auditable amounts (2-f), no duplicate invoices, per-subscription MRR that always adds up.

### D-092 · 2026-11-11 · Accepted — Sales AI learns from company-wide aggregates, shows only what the user can read
- **Decision:** Upsell co-purchase counts, discount win/loss models and "unusual for this customer" statistics read the company's decided orders with elevated rights, but only as aggregate numbers. Products, quotations and lines shown or proposed are read again as the user. Costs feed computations but never appear in a message, prompt or result.
- **Alternatives:** Statistics only over orders the user can read (too sparse for salespeople limited to their own documents).
- **Rationale:** Useful statistics without exposing other users' records (gate 1).

### D-093 · 2026-11-11 · Accepted — Discount bands are the hard limit for AI discount suggestions
- **Decision:** `sale.discount_band` picks the most specific band (deepest category, then customer segment, then larger minimum quantity, then sequence). Suggestions are rounded to 0.5 points, clamped to the band and re-checked before the proposal (one proposal per line). A user may still edit a line beyond the band; the pricing check then flags it for the band's approver group.
- **Alternatives:** Blocking out-of-band discounts on the line itself.
- **Rationale:** 100 % within band by construction without changing how sales lines behave.

### D-094 · 2026-11-11 · Accepted — For quote lines from text, the model extracts and Tessera chooses products
- **Decision:** The model returns descriptions, quantities, units, codes and constraints only. Catalogue matching is deterministic and runs as the user (code, then trigrams, then embeddings, requiring a clear lead over the runner-up); anything else is flagged, never added. Quantities must appear in the text; prices come from the pricelist.
- **Rationale:** Zero fabricated products by construction.

### D-095 · 2026-11-11 · Accepted — Spec check for AI product descriptions
- **Decision:** Any sentence stating a number or a listed specification claim (English, French, German synonyms) not present in the catalogue facts is removed before the proposal is made. Accepting writes the user's language only.
- **Rationale:** No invented specifications in customer-facing text.

### D-096 · 2026-11-11 · Accepted — Images reach a model only through the gateway, only for `vision` features without redaction
- **Decision:** The gateway request format carries base64 image or PDF blocks; the gateway refuses them for features declaring `redaction: redact` (an image cannot be redacted) or for any task class other than `vision`. The Anthropic, OpenAI and Google adapters translate them; `run_feature` takes optional `attachments`.
- **Rationale:** Bill reading needs the page, and personal data must only go to providers approved for it.

### D-097 · 2026-11-11 · Accepted — Digital documents are read deterministically first
- **Decision:** A PDF's text layer is parsed in code (`pypdf`); a model is only the fallback for scans and image-only PDFs, and its amounts are re-checked against the tax engine (and the text layer when there is one). Each reading is audited in `ai_core.run`. The whole draft bill (and a new vendor if needed) is one proposal.
- **Rationale:** Cheaper, reproducible, and the numbers come from the document.

### D-098 · 2026-11-11 · Accepted — Anomaly flags are records, not proposals
- **Decision:** `account.move_anomaly` stores flags (duplicate, unusual amount, new vendor with a large amount, changed bank account) with an explanation and dismiss / confirm; a notification is sent; posting is never blocked.
- **Rationale:** The build plan classes these as notification / flag, and the ledger rules (D-073) stay untouched.

### D-099 · 2026-11-11 · Accepted — Forecast numbers come from the engine
- **Decision:** The cash-flow forecast is computed by an engine (open items by due date adjusted by each partner's payment delay, recurring movements); a narrative quoting any number not among the engine's figures is replaced by a plain summary of the figures.
- **Rationale:** Numbers never come from a model (BUILD_PLAN §3).

### D-100 · 2026-11-11 · Accepted — Recurring-revenue AI: in-process statistics, grounded text, names are not facts
- **Decision:** Churn scores, rental demand forecasts and usage alerts are computed in-process. Churn: logistic regression with exact log-odds contributions, trained per company once it has 40 ended or surviving subscriptions (at least 10 of each), documented prior weights before that, stored as JSON artefacts with a hash check. Forecast: weekly profile × damped Theil-Sen trend on the company's own reservations, with an 80 % range. Alerts: fixed, documented rules, stored and notified, never changing other records. Generated text (renewal drafts, commission explanations, narrations) may only state numbers of the facts given; sentences with other figures are removed, and names or free-text notes are masked so digits inside them never ground a figure. Renewal drafts are message proposals. **The rental forecast threshold (MAPE ≤ 25 %) is applied to weekly units out, the planning granularity; daily MAPE (0.256 on the synthetic history) is reported for information.**
- **Alternatives:** Applying the threshold to daily figures (fails on day-to-day noise of the synthetic data); an external ML library.
- **Rationale:** Numbers come from the ledger and the statistics, never from the language model (2-k); text reachable by an injection or a mistake cannot introduce a figure. The weekly interpretation of the forecast metric is a deviation from a literal reading of BUILD_PLAN §3 and is to be confirmed at phase sign-off.

### D-023 · 2026-11-09 · Proposed — Public web surfaces before Phase 7 use `*_website` bridge modules
- **Decision:** Modules that need a public page before the `website` builder exists (recruitment job postings, event pages, eLearning courses, blog, forum, survey invitations) depend only on `portal` for their minimal public rendering in their own phase; `recruitment_website`, `events_website`, `elearning_website`, `blog_website`, `forum_website` bridge modules (auto-installed when `website` is present) move those pages into the website builder in Phase 7.
- **Rationale:** Preserves the layering rule (no dependency on a later phase) while still shipping usable public pages when each module lands.
