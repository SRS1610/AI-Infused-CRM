# Performance checks

Acceptance criterion U-6: the standard list view of each primary model returns in under 300 ms at the
95th percentile with 100,000 rows.

```bash
tessera seed demo --phase 1
```

```bash
tessera perf lists --rows 100000 --json perf.json
```

`tessera perf lists` brings contacts, leads, meetings, quotations and products up to `--rows` records by
copying the existing rows in SQL (no per-record hooks, jobs or messages run), then times the request each
model's list view sends — the view's visible columns, its default order, the first page of 80 and the
total count — as an administrator and as a salesperson whose record rules apply (`--users`). It exits
with an error when a p95 exceeds `--budget-ms`. Run it on a scratch database: it adds rows.

The nightly workflow runs it on a fresh demo database and keeps `perf.json` as an artifact. Last result
(2026-11-10, PostgreSQL 16, one API process): p95 between 17 and 108 ms for every model and user.

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
