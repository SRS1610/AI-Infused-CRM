# Phase 1 summary — foundation, CRM and AI

Template: [Build plan §2.9](architecture/BUILD_PLAN.md). State on 2026-11-10, branch
`claude/tender-wozniak-pnrg36`. The walkthrough is [docs/demos/phase-1.md](demos/phase-1.md).

## Verification

| Check | Result |
|---|---|
| Backend tests (`uv run pytest --cov`, PostgreSQL 16 + pgvector) | 435 passed, coverage 87 % |
| AI gateway tests (`gateway/`) | 118 passed, coverage 92 % |
| Frontend unit tests (`npx vitest run --coverage`) | 119 passed, coverage 90 % |
| Playwright, desktop and mobile, against `tessera seed demo` | 38 passed, 12 skipped (desktop-only scenarios on mobile), three consecutive green runs |
| `tessera ai eval report --strict` | nothing missing for any of the 21 Phase 1 features |
| AI evaluation sets in replay (1,068 cases) | every threshold met (table below) |
| Lint and types | ruff, ruff format, mypy (checked mode, strict on core packages, D-025), eslint, prettier, tsc: clean |
| `tessera seed demo --phase 1` on an empty database | ~13 s, `tessera db check` clean |

## Works end to end

- **Kernel**: metadata ORM over SQLAlchemy Core (fields, computed and related fields, constraints,
  inheritance by fragments), module install / upgrade / uninstall with `tessera db check`, record rules,
  field groups, multi-company, multi-currency, translations, audit log
  (tests: `backend/tessera/*/tests`, `backend/tessera/testing/modules/kernel_test_*`).
- **API and client**: REST and JSON-RPC for every model under the caller's permissions, OpenAPI, sessions
  with TOTP and OIDC, API keys, ETags, rate limits per credential (D-059), realtime over WebSocket; React
  client with list, form, kanban, calendar, pivot, graph and search views, command palette, dark mode,
  French catalogue (`backend/tessera/api/tests`, `frontend/src/**/*.test.tsx`).
- **Platform services**: chatter (messages, followers, activities, mentions, e-mail in and out), jobs and
  scheduler, workflow engine and automated actions, report templates and PDF layouts, import with dry run,
  export, webhooks (`backend/modules/{chatter,workflow,reports}/tests`, `backend/tessera/dataio/tests`).
- **Applications**: Contacts (companies, people, merge and undo), Discuss (channels, direct messages,
  presence), Calendar (rooms with conflict checks, recurrence, invitations, booking links), CRM (leads,
  pipeline, teams, assignment, intake by e-mail and web form, conversion, lost reasons, reporting),
  Products, Quotations with PDF and customer portal acceptance (`backend/modules/*/tests`,
  `frontend/e2e/apps.spec.ts`).
- **AI platform**: gateway service with Anthropic, OpenAI (also Azure and compatible), Gemini, Voyage and
  a deterministic test adapter; routing per company and task class, fallbacks, circuit breaker, cache,
  budgets, PII redaction, schema validation, kill switches, ledger, metrics; runtime with tools under the
  user's permissions, retrieval filtered by record rules before ranking, signed proposals, run audit,
  evaluation harness (`gateway/tests`, `backend/modules/ai_core/tests`, `backend/tessera/ai`).
- **AI features**: the 21 features below, each behind a per-company flag, writing only through proposals
  a person accepts (`backend/modules/*/tests/test_ai_*.py`, `frontend/e2e/ai.spec.ts`,
  `frontend/e2e/demo.spec.ts`).
- **Studio**: add fields of every type to any Studio-enabled model without a restart, place them on the
  form and list, edit the form layout, automated actions and simple workflows, change history with
  rollback per change or to a snapshot, export as an installable module, designers only
  (`backend/modules/studio/tests/test_studio.py` covers 1.7-a, b, d, e; `frontend/e2e/studio.spec.ts`
  covers 1.7-a, b, c in the browser).
- **Demo**: `tessera seed demo --phase 1` and the scripted walkthrough, executed by
  `frontend/e2e/demo.spec.ts`.

## Stubbed or partial

- **Model output**: no provider key was available, so every AI answer in tests and in the demo comes
  from the deterministic test adapter. LLM evaluation scores replay hand-authored answers and judge
  scores (D-050, D-058): they test validation, citations, permissions, proposals and injection handling,
  not model quality. Scoring, risk, routing, forecast, duplicates, translation and the other measured
  features run the production code on generated data.
- **Live evaluation (1.6-b)**: `tessera ai eval run --mode live` exists; the nightly live run has not been
  executed (needs provider keys).
- **Contact enrichment**: only the offline fixture adapter ships (D-057).
- **OIDC**: tested against a mocked identity provider; no real provider configured.
- **Calendar sync**: CalDAV, Google Calendar and Microsoft 365 adapters sync incrementally, tested
  against fake provider servers; OAuth token refresh for Google and Microsoft is not implemented, so a
  connection needs a fresh token when the stored one expires.
- **Presence and realtime**: realtime messages fan out across processes through Redis pub/sub
  (`TESSERA_REALTIME_BACKEND=redis`), tested with the in-process backend only; presence counts the
  WebSocket connections of the process that answers, so it is partial with several API processes.
- **Studio**: layout editing covers the form (order, visibility, read-only); list, kanban and search views
  change through view operations in the API. The workflow builder is API-only. The registry rebuild runs
  under a lock in the requesting process (D-053, D-060).
- **Server-side translations**: the client catalogue `fr_FR` is complete (645/645); menu names, field
  labels and server messages are English only, so a French user sees mixed languages (U-8 partial).

## Known gaps

| Gap | Impact | Planned |
|---|---|---|
| Docker Compose stack not started in the build environment | U-1 checked by running the services directly; the compose files are untested here | before the Phase 1 sign-off, on a machine with Docker |
| U-6 nightly performance job (100k rows, p95 < 300 ms) not run | list performance at volume unmeasured | first nightly run |
| U-9 OWASP ASVS L2 checklist delta not reviewed | no known high-severity finding, but no formal review | before Phase 1 sign-off |
| Server strings not translatable to French (U-8) | mixed-language interface for French users | Phase 2 (extraction of server strings) |
| Lost deals are not in the retrieval index | objection handling cites won deals and notes only | Phase 2 |
| Price check in drafts misses bare amounts without a currency | a price typed as a plain number is not flagged | Phase 2 |
| Evaluation sets are tied to their corpus | `tessera ai eval run` against a demo or production database can miss citation thresholds (the corpus differs); CI runs them on the test database | document; corpus isolation in Phase 2 |
| Backend suite takes ~8 minutes serially | slower CI feedback | parallel test databases |

## AI features

Replay scores on the evaluation corpus (CI). No live run yet; acceptance rates appear on **Settings → AI →
Usage and cost** once people use the features, and no real user sessions exist yet.

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
| `crm.followup_draft` | per company, on | rubric 0.92 ≥ 0.80; price grounding 1.0 = 1.0 | not run | n/a |
| `crm.call_summary` | per company, on | rubric 0.94 ≥ 0.84; field F1 0.91 ≥ 0.85 | not run | n/a |
| `crm.objections` | per company, on | rubric 0.92 ≥ 0.80; citations 1.0 = 1.0 | not run | n/a |
| `crm.nl_search` | per company, on | domain match 0.94 ≥ 0.85 | not run | n/a |

Every feature also scores injection resistance 1.0 (threshold 1.0) and, where it returns structured
output, schema validity 1.0. Replay latency p95 is at most 228 ms per feature on the test adapter (1.6-f
budget: 3 s); the first streamed token arrives in under 1.5 s in the browser (`frontend/e2e/ai.spec.ts`).
