# Phase 2 summary — sales and accounting

Template: [Build plan §2.9](architecture/BUILD_PLAN.md). State on 2026-11-12, branch
`claude/tender-wozniak-pnrg36`. The walkthrough is [docs/demos/phase-2.md](demos/phase-2.md); the data model
is [ERD_PHASE_2.md](architecture/ERD_PHASE_2.md); decisions D-069 – D-101.

Modules: `product` 2.0, `sale` 2.0, `sale_subscription`, `sale_rental`, `sale_commission`, `account`,
`account_bank`, `account_tax`, `account_analytic`, `account_budget`, `account_asset`, `account_reports`,
`account_dunning`, `account_einvoice`, `payment`, `auth_saml`.

## Acceptance criteria

### Universal (BUILD_PLAN §1.4)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| U-1 | Fresh clone → `docker compose up` → demo admin → every menu opens without console errors | **Met** | Docker Compose stack, `tessera seed demo --phase 2`, then a browser opened all 163 Phase 1 + 2 menus: no console error, no server error, no error banner, no serious / critical axe violation (same check on the development server) |
| U-2 | Every module installs on an empty database; leaf modules uninstall; `tessera db check` clean | **Met** | install of every Phase 2 module on an empty database, uninstall of each leaf (`sale_rental`, `sale_commission`, `account_dunning`, `account_einvoice`, `account_budget`, `account_asset`, `auth_saml`, …), `tessera db check`: no drift |
| U-3 | A database seeded at Phase 1, after `tessera db migrate`, passes the current suite | **{{U3_STATUS}}** | {{U3_EVIDENCE}} |
| U-4 | A user of company A sees no company-B record anywhere, AI retrieval included | **Met** | `test_sec_phase2_every_company_model_is_isolated` (every Phase 2 model has a stored `company_id` or is listed as shared, an outsider with the widest roles sees nothing), cross-company references refused by the ledger (journal, partner, accounts, bank matches, reports), AI gate tests per feature (`isolation` metric 1.0) |
| U-5 | Restricted fields absent from read, export, API, OpenAPI and AI tool results | **Met** | `test_u5_phase2_restricted_fields_are_absent` (payment provider secrets, invoice access token, a quotation's invoices), on the Phase 1 field-group mechanism (`kernel_test_a` tests, API `/fields`, export); the OpenAPI schema is generic and lists no model fields |
| U-6 | List views of each primary model at 100k rows: p95 < 300 ms | **Met** | `tessera perf lists`: p95 ≤ 118 ms for contacts, leads, meetings, orders, products, journal items, journal entries, payments, bank transactions, fixed assets (`docs/admin/performance.md`) |
| U-7 | Axe: no serious / critical violation on every new view type and module home | **Met** | the U-1 crawl runs axe on every menu; `frontend/e2e/account.spec.ts` checks the accounting views, the reconciliation screen and the reports |
| U-8 | Every user-facing string translatable; `fr_FR` ≥ 95 % | **Met** | server 4,832 / 4,832 strings (`tessera i18n stats`, test fails under 95 %), client catalogue complete |
| U-9 | ASVS L2 delta reviewed; no high-severity finding open | **Met** | [asvs-phase-2.md](security/asvs-phase-2.md): 68 requirements; 3 high, 3 medium and 1 low fixed with regression tests (SEC-50 – SEC-56, D-101); 6 low open and scheduled (SEC-57 – SEC-62) |
| U-10 | Metrics for every job and AI feature | **Met** | new jobs (FX revaluation, bank sync, dunning, subscriptions, rentals, anomaly checks, Peppol status, churn scoring) and the 22 features report through the Phase 1 job and AI-run metrics on `GET /metrics` |
| U-11 | All AI features pass the §1.3 gates | **Met in replay** | `tessera ai eval run` (all thresholds) and `tessera ai eval report --strict` (nothing missing); live run pending, as in Phase 1 |

### Phase 2 (BUILD_PLAN §3)

| # | Criterion | Status | Evidence |
|---|---|---|---|
| 2-a | Trial balance sums to zero per company and consolidated; P&L and balance sheet reconcile | **Met** | `backend/modules/account_reports/tests/test_acceptance.py` on the demo data |
| 2-b | Gapless per-journal, per-year numbers; cancelling refused once the period is locked | **Met** | `backend/modules/account/tests/test_ledger.py` (numbering, deleted drafts use no number, lock dates, hash chain) |
| 2-c | Realised FX on a USD invoice paid later; reversible unrealised FX entries | **Met** | `backend/modules/account/tests/test_payments.py` |
| 2-d | CAMT.053, MT940, OFX and CSV import without loss; demo CAMT.053 ≥ 90 % auto-reconciled; the rest proposed by AI | **Met** | `backend/modules/account_bank/tests/test_import.py`, `test_rules.py` (demo statement reconciled by the shipped rules above 90 %, the remainder proposed by `account_bank.reconcile_suggest`) |
| 2-e | Tax engine on a 40-case golden set; tax return ties to the ledger | **Met** | `backend/modules/account/tests/test_taxes.py` (40 cases: included, excluded, cascading, groups, fiscal positions, reverse charge), `backend/modules/account_tax/tests/test_tax_return.py` |
| 2-f | Subscription 20-scenario fixture; rental calendar prevents double-booking | **Met** | `backend/modules/sale_subscription/tests/test_scenarios.py`, `backend/modules/sale_rental/tests/test_availability.py` (application check and a GiST exclusion constraint, also against raw SQL) |
| 2-g | Straight-line and declining schedules match the golden table; disposal posts gain / loss | **Met** | `backend/modules/account_asset/tests/test_golden.py` |
| 2-h | UBL 2.1 validates against the official schematron; Peppol mock round-trips | **Met** | `backend/modules/account_einvoice/tests/test_ubl.py` (OASIS UBL 2.1 XSD, EN 16931 and OpenPeppol BIS 3 schematron via Saxon-HE; the vendored XSLT is checked against the official `.sch`) |
| 2-i | OCR eval set ≥ 100 bills, multi-language, scans and digital, meets thresholds; every auto-coded line is a proposal | **Met in replay** | `account.bill_ocr` evaluation (scores below), `backend/modules/account/tests/test_ai_bill.py`; synthetic documents (no real supplier documents in the corpus) |
| 2-j | Stripe test adapter payment reconciles the invoice; webhook replay idempotent | **Met** | `backend/modules/payment/tests/test_flow.py`, `test_api.py` (signed webhooks, replay is a no-op) |
| 2-k | Commission statement equals the spreadsheet cross-check; AI explanation cites only statement numbers | **Met** | `backend/modules/sale_commission/tests/test_crosscheck.py` (independent computation), `test_ai.py` (numeric consistency 1.0) |
| 2-l | Phase 1 database upgraded passes the suite (U-3); reports on 100k lines < 2 s p95 | **{{U3_STATUS}}** | U-3 above; `tessera perf reports --lines 100000`: p95 between 55 ms (partner ledger) and 432 ms (general ledger, first page) |

## Verification

| Check | Result |
|---|---|
| Backend tests (`uv run pytest --cov`, PostgreSQL 16 + pgvector) | 1,378 passed, coverage 91 % |
| AI gateway tests (`gateway/`) | 121 passed, 5 skipped (live provider tests without keys), coverage 94 % |
| Frontend unit tests (`npx vitest run --coverage`) | 221 passed, coverage 92 % |
| Playwright, desktop and mobile, against `tessera seed demo --phase 2` | {{PLAYWRIGHT}} |
| U-3: Phase 1 database (commit `1a1fada`) → `tessera db migrate` → current suite | {{U3_EVIDENCE}} |
| AI evaluation sets in replay | {{EVAL_SUMMARY}} |
| `tessera ai eval report --strict` | {{REPORT_STRICT}} |
| Lint and types | ruff, ruff format, mypy (`tessera`, CI scope), eslint, prettier, tsc: clean |
| Docker Compose | builds, starts, seeds Phase 2 and serves the demo (see U-1) |

## Works end to end

- **Products and sales**: variants as child products (D-071), attributes with price extras, pricelists by
  customer, region, quantity and date, promotions and coupon codes, discount bands with approval, quotation
  templates with optional products, sales orders with ordered / delivered invoicing, milestones and down
  payments, customer returns as credit notes.
- **Accounting core**: US, EU and generic charts per company; journals; fiscal years (also across calendar
  years) with closing and lock dates; invoices, credit notes, bills and refunds with gapless numbering,
  SHA-256 hash chains and database-level balance checks (D-073); tax engine with groups, cascading,
  price-included taxes, fiscal positions and reverse charge; payment terms with early-payment discounts;
  partial and batch payments, write-offs, reconciliation with realised FX, monthly unrealised FX revaluation;
  invoices e-mailed with a portal link.
- **Bank**: CSV, OFX, CAMT.053 and MT940 import, reconciliation rules, manual matching screen with AI
  suggestions (D-085).
- **Payments**: provider adapter interface with Stripe (REST, signed webhooks recorded once) and a test
  provider; portal invoice payment, saved cards, refunds (D-086).
- **Management accounting**: analytic plans and distributions, budgets with budget-vs-actual, fixed assets
  (straight-line, declining, declining-then-linear; disposal and changes), financial reports (general ledger,
  trial balance, P&L, balance sheet, cash flow, aged receivable / payable, partner ledger, consolidation)
  with PDF / XLSX / CSV export, tax return with closing entry (D-082, D-083).
- **Collections and e-invoicing**: dunning levels and runs with letters and e-mails (D-088); UBL 2.1 /
  Peppol BIS 3 generation validated with the official artefacts, a Peppol access-point interface with a
  mock that sends and receives (D-087).
- **Recurring revenue**: subscriptions billed on an anchored grid with proration, renewals, close reasons and
  MRR (D-091); rentals priced by covering duration with unit reservations, late fees and deposits (D-089);
  commission plans and statements (D-090).
- **SAML single sign-on** (`auth_saml`, D-076).
- **AI features**: the 22 features below.
- **Upgrade path**: `tessera db migrate` installs new dependencies, loads new demo records on databases with
  demo data (`missing` loader mode), and fills static defaults on added columns.

## Stubbed or partial

- **Model output**: as in Phase 1, no provider key was available, so every AI answer in tests and in the demo
  comes from the deterministic test adapter; LLM scores replay hand-authored answers and judge scores.
  Statistical features (forecasts, churn, anomalies, matching, classification) are measured for real on
  generated data.
- **Evaluation data is synthetic**: bills for OCR (four layouts, four languages, scans rendered from them),
  bank statements, subscription and rental histories. Real documents will more often need the model or a
  correction.
- **Peppol**: one access-point interface with a mock; no certified access point and no national CIUS rules
  (XRechnung, Factur-X, …).
- **Returns and delivered quantities** are entered by hand until stock arrives (Phase 3).
- **Consolidation** adds companies' balances in the reporting currency; there are no intercompany
  eliminations.
- **Analytic distributions** are edited as a percentage table; there is no dedicated widget with plan
  totals.
- **Production web image**: built and checked in CI; in this build environment the base image pull hit a
  registry rate limit, so U-1 used the development web target.

## Known gaps

| Gap | Impact | Planned |
|---|---|---|
| Live AI evaluation not yet run (Phase 1 and Phase 2 features) | model quality on real providers unmeasured | first nightly run after the `ANTHROPIC_API_KEY` repository secret is added |
| `sale_rental.demand_forecast` threshold applied to weekly figures (MAPE ≤ 25 %); daily MAPE is 0.256 (D-100) | a deviation from the literal reading of BUILD_PLAN §3 | **to confirm at phase sign-off** |
| 6 low ASVS findings (SEC-57 – SEC-62): refunds and card charges open to every billing user, Stripe test mode offered in production, statement files through the generic API not size-capped, a few configuration children without company, customer links that never expire, internal readers can close a subscription through the customer endpoint | documented, none high or medium | Phase 3 — [asvs-phase-2.md](security/asvs-phase-2.md) §2.2 |
| Intercompany eliminations, national e-invoicing formats, certified Peppol access point | multi-entity groups and some countries need manual work | later phases |
| Backend suite takes ~85 minutes serially with coverage | slow CI feedback | parallel test databases |

## AI features

Replay scores on the evaluation corpus (CI). No live run yet; acceptance rates appear under **Settings → AI
→ Usage and cost** once people use the features. Every feature has a per-company switch (on by default) and
gate tests: switched off it answers `404 feature_disabled` without calling the gateway; it reads as the
user; nothing is written before a proposal is accepted; each run is in `ai_core.run`.

| feature | flag | eval score vs threshold | live run date | acceptance rate (demo) |
|---|---|---|---|---|
{{AI_TABLE}}

Every feature also scores injection resistance 1.0 (threshold 1.0) where it reads free text, and schema
validity 1.0 where it returns structured output.
