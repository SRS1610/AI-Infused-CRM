# Performance checks

Acceptance criterion U-6: the standard list view of each primary model returns in under 300 ms at the
95th percentile with 100,000 rows.

```bash
tessera seed demo --phase 2
```

```bash
tessera perf lists --rows 100000 --json perf.json
```

`tessera perf lists` brings contacts, leads, meetings, quotations (and sales orders), products, journal
entries, journal items, payments, bank transactions and fixed assets up to `--rows` records by copying the
existing rows in SQL (no per-record hooks, jobs or messages run; journal items are copied as whole balanced
entries, since the ledger refuses anything else), then times the request each
model's list view sends — the view's visible columns, its default order, the first page of 80 and the
total count — as an administrator and as a salesperson whose record rules apply (`--users`). It exits
with an error when a p95 exceeds `--budget-ms`. Run it on a scratch database: it adds rows.

The nightly workflow runs it on a fresh demo database and keeps `perf.json` as an artifact. Models a user
cannot open are skipped for that user.

Last result (2026-11-11, Phase 2 demo, PostgreSQL 16, one API process), p95 in ms:

| Model | Administrator | Salesperson (`demo`) |
|---|---|---|
| Contacts | 104.7 | 96.5 |
| Leads | 118.0 | 118.2 |
| Meetings | 68.9 | 85.3 |
| Quotations and orders | 96.6 | 17.8 (sees only their own) |
| Products | 90.1 | 84.1 |
| Journal items | 64.5 | no access |
| Journal entries and invoices | 48.6 | no access |
| Payments | 65.9 | no access |
| Bank transactions | 75.8 | no access |
| Fixed assets | 81.2 | no access |

## Financial reports

Acceptance criterion 2-l: `account` reports on 100,000 journal items render in under 2 s at the 95th
percentile.

```bash
tessera seed demo --phase 2
tessera perf reports --lines 100000 --json perf-reports.json
```

`tessera perf reports` brings a company (default: the main company) up to `--lines` posted journal items
by copying its posted entries in SQL — whole entries, so every copy balances, dated back over the previous
two years — then times, through the API as `--login` (an accounting user), the trial balance, profit and
loss, balance sheet, general ledger (first page: every account unfolded with its first 80 items), aged
receivable and partner ledger over the last two years and a half. It exits with an error when a p95
exceeds `--budget-ms` (2,000). The nightly workflow runs it as the *perf-reports* job and keeps
`perf-reports.json`.

Last result (2026-11-10, PostgreSQL 16, one API process, 100,056 posted items, 20 runs each):

| Report | p50 | p95 |
|---|---|---|
| Trial balance | 52 ms | 66 ms |
| Profit and loss | 47 ms | 64 ms |
| Balance sheet | 101 ms | 149 ms |
| General ledger (first page, 914 lines) | 362 ms | 432 ms |
| Aged receivable | 94 ms | 107 ms |
| Partner ledger | 45 ms | 55 ms |

No new index was needed: the queries group by account over `(company_id, date)` and use the `account`
module's indexes on accounts, partners, reconciliation and matches.

## Database sessions

The application turns PostgreSQL's JIT compiler off for its own sessions (`TESSERA_DB_JIT=false`, the
default): on a 100k-row table JIT added ~160 ms of compilation to a 20 ms count. Analytical tools that
connect separately keep the server's setting. Each session also gets `statement_timeout`
(`TESSERA_DB_STATEMENT_TIMEOUT_MS`, 60 s) and the UTC time zone.

## Monitoring

`GET /metrics` on the API serves Prometheus metrics (U-10):

| Metric | Labels | What |
|---|---|---|
| `tessera_http_requests_total`, `tessera_http_request_seconds` | method, route template, status class | API traffic and latency |
| `tessera_jobs_total`, `tessera_job_seconds` | job, state (`done`, `retry`, `dead`) | background job attempts and run time |
| `tessera_ai_runs_total` | feature, status | AI feature runs and their outcome (`ok`, `error`, `feature_disabled`, …) |
| `tessera_ai_tokens_total`, `tessera_ai_cost_usd_total`, `tessera_ai_latency_seconds` | feature | tokens, provider cost and provider time per feature |
| `tessera_ai_gateway_*` | feature, provider, model | the gateway's own metrics, included when it runs in-process |

A separate gateway service serves its metrics on its own `/metrics` (port 8100). In development the
endpoint is open; in production it is served only with `Authorization: Bearer $TESSERA_METRICS_TOKEN`
(no token configured → not served). Each process (API, worker) keeps its own counters: scrape every
process, or run jobs in the API process for a small deployment.
