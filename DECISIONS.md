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

### D-023 · 2026-11-09 · Proposed — Public web surfaces before Phase 7 use `*_website` bridge modules
- **Decision:** Modules that need a public page before the `website` builder exists (recruitment job postings, event pages, eLearning courses, blog, forum, survey invitations) depend only on `portal` for their minimal public rendering in their own phase; `recruitment_website`, `events_website`, `elearning_website`, `blog_website`, `forum_website` bridge modules (auto-installed when `website` is present) move those pages into the website builder in Phase 7.
- **Rationale:** Preserves the layering rule (no dependency on a later phase) while still shipping usable public pages when each module lands.
