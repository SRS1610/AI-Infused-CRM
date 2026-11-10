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
