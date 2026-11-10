# Phase 1 summary — foundation, CRM and AI

Template: [Build plan §2.9](architecture/BUILD_PLAN.md). State on 2026-11-10, branch
`claude/tender-wozniak-pnrg36`. The walkthrough is [docs/demos/phase-1.md](demos/phase-1.md).

## Acceptance criteria

| # | Criterion | Status | Evidence |
|---|---|---|---|
| U-1 | Fresh clone → `docker compose up` → demo admin → every menu opens without console errors | **Met** | `make up`, `tessera seed demo --phase 1` (through the gateway service), then a browser opened all 74 Phase 1 menus with no console error, no server error and no error banner; an attachment upload went through the S3 object store |
| U-2 | Every module installs on an empty database; leaf modules uninstall; `tessera db check` clean | **Met** | `backend/tessera/modules/tests`, module lifecycle tests (D-052) |
| U-3 | Upgrade path from the previous phase | n/a | Phase 1 is the first phase; Calendar and Contacts already carry 1.1.0 upgrade scripts |
| U-4 | A user restricted to company A sees no company-B record anywhere, AI retrieval included | **Met** | cross-company tests in every module, retrieval filtered before ranking; context company widening closed (SEC-01) |
| U-5 | Restricted fields absent from read, export, API, OpenAPI and AI tool results | **Met** | `backend/tessera/testing/modules/kernel_test_a/tests/test_security.py`, AI tool tests |
| U-6 | List views of each primary model at 100k rows: p95 < 300 ms | **Met** | `tessera perf lists`: p95 between 17 and 108 ms for contacts, leads, meetings, quotations and products, as administrator and as a salesperson under record rules (`docs/admin/performance.md`); nightly job |
| U-7 | Axe: no serious/critical violation on every new view type and module home | **Met** | Playwright + axe in `frontend/e2e/*.spec.ts` (Studio tabs included) |
| U-8 | Every user-facing string translatable; `fr_FR` ≥ 95 % | **Met** | server 2,184 / 2,184 strings (test fails under 95 %), client 765 / 765 |
| U-9 | ASVS L2 delta reviewed; no high-severity finding open | **Met** | `docs/security/asvs-phase-1.md`: 209 requirements; 7 high findings fixed with regression tests; 6 medium and 16 low open and scheduled |
| U-10 | Metrics for every job and AI feature | **Met** | `GET /metrics` on the API: requests, jobs (count, run time, outcome) and AI feature runs (outcome, tokens, cost, latency), plus the gateway's metrics (`docs/admin/performance.md`); `tessera/api/tests/test_metrics.py` |
| U-11 | All AI features pass the §1.3 gates | **Met in replay** | `tessera ai eval report --strict`; live run pending (below) |
| 1.6-b | Thresholds pass in replay CI; nightly live run reports accuracy, latency p95, cost | **Replay met; live not yet run** | `.github/workflows/nightly.yml` runs it once the `ANTHROPIC_API_KEY` repository secret is set |
| 1.7-a–e | Studio | **Met** | `backend/modules/studio/tests`, `frontend/e2e/studio.spec.ts` |

## Verification

| Check | Result |
|---|---|
| Backend tests (`uv run pytest --cov`, PostgreSQL 16 + pgvector) | 590 passed, coverage 88 % |
| AI gateway tests (`gateway/`) | 118 passed, coverage 92 % |
| Frontend unit tests (`npx vitest run --coverage`) | 140 passed, coverage 91 % |
| Playwright, desktop and mobile, against `tessera seed demo` | 40 passed, 14 skipped (desktop-only scenarios on mobile) |
| AI evaluation sets in replay (1,076 cases) | all 21 features meet every threshold, on the test database and on a demo database (corpus sandbox, D-063) |
| `tessera ai eval report --strict` | nothing missing |
| Lint and types | ruff, ruff format, mypy (`tessera`, CI scope; checked mode, strict on core packages, D-025), eslint, prettier, tsc: clean |
| Docker Compose | builds, starts (db, redis, object store, mailpit, API, worker, scheduler, gateway, web), seeds and serves the demo |

## Works end to end

- **Kernel**: metadata ORM (fields, computed and related fields, constraints, fragments), module install /
  upgrade / uninstall with `tessera db check`, record rules, field groups, multi-company, multi-currency,
  translatable values, audit log; registries reload when another process changes modules or Studio fields
  (D-053, D-065).
- **API and client**: REST and JSON-RPC under the caller's permissions, OpenAPI, sessions with TOTP and
  OIDC, API keys, ETags, per-credential rate limits with `Retry-After` (D-059), realtime over WebSocket
  (presence across processes with Redis); React client with list, form, kanban, calendar, pivot and graph
  views, command palette, dark mode, English and French.
- **Translations**: per-module gettext catalogues, `tessera i18n extract | stats | load`, French complete
  for server and client (D-061).
- **Platform services**: chatter, e-mail in and out, jobs and scheduler, workflows and automated actions,
  report templates and PDF layouts, import with dry run, export, webhooks.
- **Applications**: Contacts (merge and undo, company website enrichment), Discuss, Calendar (rooms,
  recurrence, invitations, booking links, CalDAV / Google / Microsoft sync with OAuth token refresh), CRM,
  Products, Quotations with PDF and customer acceptance.
- **AI platform**: gateway (Anthropic, OpenAI / Azure / compatible, Gemini, Voyage, deterministic test
  adapter), routing, fallbacks, budgets, PII redaction, validation, kill switches, ledger, metrics;
  runtime with permission-scoped tools, retrieval filtered before ranking, proposals, audit, evaluation
  harness with a corpus sandbox; `tessera ai provider` (D-016 tiers).
- **AI features**: the 21 features below.
- **Studio**: fields of every type with widget, default, conditions and groups; field edits; form, list
  and kanban layouts; new views; search filters; automated actions; workflow builder; history with
  rollback per change or to a snapshot; export as a module (D-053, D-054, D-062).
- **Operations**: Docker Compose stack (D-066), `tessera seed demo`, `tessera perf lists`, nightly
  workflow (performance; live AI evaluation when a key is configured).

## Stubbed or partial

- **Model output**: no provider key was available in the build environment, so every AI answer in tests
  and in the demo comes from the deterministic test adapter. LLM scores replay hand-authored answers and
  judge scores (D-050, D-058); they test validation, citations, permissions, proposals and injection
  handling, not model quality. Statistical features are measured for real on generated data.
- **Calendar OAuth**: tokens refresh automatically, but there is no in-app "connect your account"
  (authorise / callback) screen yet; tokens are entered on the account.
- **Studio**: the workflow overview is an ordered list, not a diagram; fields hidden in the form editor are
  re-added from the list and kanban editors, not from the form editor; menus pinned to a specific view keep
  it after a new default view is created.
- **Translations**: some stored machine-generated text (deal-risk reasons, forecast narratives, Studio
  change descriptions) is written once in English; month names in printed dates are not localised.
- **Evaluations** need the demo users, companies and products they name; a database without demo data
  cannot run them.

## Known gaps

| Gap | Impact | Planned |
|---|---|---|
| Live AI evaluation not yet run (1.6-b) | model quality on real providers unmeasured | first nightly run after the `ANTHROPIC_API_KEY` secret is added |
| 6 medium and 16 low ASVS findings open (session lifetimes, 8-character minimum passwords, no enforced 2FA for administrators, business-model rules checked before writes only, designer automations with system rights inside business models, no CSP on the app shell, …) | documented, none high | Phase 2 (most), Phase 3 (upload hygiene) — `docs/security/asvs-phase-1.md` §2.2 |
| Docker images rely on Debian packages and GitHub's container registry (uv) | a mirror needs `UV_IMAGE` / apt configuration | as needed |
| `mypy tessera modules` reports pre-existing errors in module code (CI checks `tessera`) | weaker typing in modules | tighten per module (D-025) |
| Backend suite takes ~17 minutes serially | slower CI feedback | parallel test databases |

## AI features

Replay scores on the evaluation corpus (CI, and the same on a demo database). No live run yet;
acceptance rates appear under **Settings → AI → Usage and cost** once people use the features.

| feature | flag | eval score vs threshold | live run date | acceptance rate (demo) |
|---|---|---|---|---|
| `discuss.summary` | per company, on | rubric 0.96 ≥ 0.84; citations 1.0 = 1.0 | not run | n/a |
| `discuss.catch_up` | per company, on | contains 0.90 ≥ 0.85; citations 1.0 = 1.0 | not run | n/a |
| `discuss.reply_draft` | per company, on | rubric 0.93 ≥ 0.80; contains 1.0 ≥ 0.9 | not run | n/a |
| `discuss.translate` | per company, on | chrF 0.73 ≥ 0.60 (each pair ≥ 0.68) | not run | n/a |
| `chatter.daily_todo` | per company, on | rank correlation 0.70 ≥ 0.60 | not run | n/a |
| `calendar.meeting_brief` | per company, on | rubric 0.89 ≥ 0.80; unauthorised facts 0 = 0 | not run | n/a |
| `calendar.suggest_times` | per company, on | valid top 3 1.0 ≥ 0.95 | not run | n/a |
| `calendar.nl_schedule` | per company, on | field F1 0.997 ≥ 0.90 | not run | n/a |
| `contacts.duplicates` | per company, on | precision 0.96 ≥ 0.90; recall 1.0 ≥ 0.80 | not run | n/a |
| `contacts.tag_suggest` | per company, on | top-3 accuracy 0.97 ≥ 0.80 | not run | n/a |
| `contacts.enrich` | per company, on | field accuracy 0.98 ≥ 0.90; attribution 1.0 = 1.0 | not run | n/a |
| `crm.lead_score` | per company, on | AUC 0.88 ≥ 0.75; factor validity 1.0 ≥ 0.9 | not run | n/a |
| `crm.next_best_action` | per company, on | rubric 0.88 ≥ 0.80; citations 1.0 = 1.0 | not run | n/a |
| `crm.deal_risk` | per company, on | precision 0.83 ≥ 0.70 | not run | n/a |
| `crm.lead_routing` | per company, on | accuracy 0.86 ≥ 0.80; zero mutations 1.0 | not run | n/a |
| `crm.forecast` | per company, on | MAPE 0.13 ≤ 0.20; P10–P90 coverage 0.88 ≥ 0.80 | not run | n/a |
| `crm.lead_extract` | per company, on | field F1 0.99 ≥ 0.90 | not run | n/a |
| `crm.followup_draft` | per company, on | rubric 0.92 ≥ 0.80; price grounding 1.0 = 1.0; price check 1.0 = 1.0 | not run | n/a |
| `crm.call_summary` | per company, on | rubric 0.94 ≥ 0.84; field F1 0.91 ≥ 0.85 | not run | n/a |
| `crm.objections` | per company, on | rubric 0.92 ≥ 0.80; citations 1.0 = 1.0 | not run | n/a |
| `crm.nl_search` | per company, on | domain match 0.94 ≥ 0.85 | not run | n/a |

Every feature also scores injection resistance 1.0 (threshold 1.0) and, where it returns structured
output, schema validity 1.0. Replay latency p95 is at most 228 ms per feature on the test adapter (1.6-f
budget: 3 s); the first streamed token arrives in under 1.5 s in the browser.
