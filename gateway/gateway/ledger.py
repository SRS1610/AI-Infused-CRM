"""The usage and cost ledger: one row per provider-facing call (and per blocked call), the source of
budgets, quotas and the administrator's cost dashboard.

``SqlLedger`` writes to schema ``ai_gateway`` in the application database (AI architecture note
§8, default); the gateway creates the schema itself, so the application's schema sync never sees
it. ``MemoryLedger`` serves tests.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Protocol

GROUP_KEYS = ("company_id", "feature", "provider", "model", "user_id", "task_class", "kind", "day")


@dataclass
class Entry:
    company_id: int
    feature: str
    task_class: str
    kind: str  # complete | stream | embed
    provider: str = ""
    model: str = ""
    user_id: int | None = None
    prompt_version: str = ""
    run_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: Decimal = Decimal(0)
    latency_ms: int = 0
    # ok | refusal | invalid_output | provider_error | budget_exceeded | quota_exceeded | feature_disabled | …
    status: str = "ok"
    fallback: bool = False
    cached: bool = False
    redacted: bool = False
    inference_geo: str | None = None
    retention: str = ""
    error: str = ""
    ts: dt.datetime = field(default_factory=lambda: dt.datetime.now(dt.UTC))

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class Totals:
    cost: Decimal = Decimal(0)
    tokens: int = 0
    requests: int = 0


def period_start(period: str, now: dt.datetime | None = None) -> dt.datetime:
    now = now or dt.datetime.now(dt.UTC)
    if period == "day":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


BILLABLE = ("ok", "refusal", "invalid_output")


class Ledger(Protocol):
    async def record(self, entry: Entry) -> None: ...

    async def totals(
        self, company_id: int, since: dt.datetime, *, user_id: int | None = None, feature: str | None = None
    ) -> Totals: ...

    async def report(
        self, *, company_id: int | None, since: dt.datetime | None, until: dt.datetime | None, group_by: list[str]
    ) -> list[dict[str, Any]]: ...


def _aggregate(rows: list[Entry], group_by: list[str]) -> list[dict[str, Any]]:
    groups: dict[tuple[Any, ...], list[Entry]] = {}
    for e in rows:
        key = tuple(e.ts.date().isoformat() if g == "day" else getattr(e, g) for g in group_by)
        groups.setdefault(key, []).append(e)
    out = []
    for key, items in sorted(groups.items(), key=lambda kv: tuple(str(k) for k in kv[0])):
        ok = [e for e in items if e.status in BILLABLE]
        lat = sorted(e.latency_ms for e in ok if not e.cached) or [0]
        row: dict[str, Any] = dict(zip(group_by, key, strict=True))
        row.update(
            requests=len(ok),
            blocked=len(items) - len(ok),
            input_tokens=sum(e.input_tokens for e in ok),
            output_tokens=sum(e.output_tokens for e in ok),
            cache_read_tokens=sum(e.cache_read_tokens for e in ok),
            cache_write_tokens=sum(e.cache_write_tokens for e in ok),
            cost=str(sum((e.cost for e in ok), Decimal(0))),
            latency_p50=float(statistics.median(lat)),
            latency_p95=float(lat[min(len(lat) - 1, max(0, round(0.95 * len(lat) + 0.5) - 1))]),
            fallback_rate=round(sum(e.fallback for e in ok) / len(ok), 4) if ok else 0.0,
            cache_hit_rate=round(sum(e.cached for e in ok) / len(ok), 4) if ok else 0.0,
            errors=sum(e.status != "ok" for e in ok),
        )
        out.append(row)
    return out


class MemoryLedger:
    def __init__(self) -> None:
        self.entries: list[Entry] = []

    async def record(self, entry: Entry) -> None:
        self.entries.append(entry)

    async def totals(
        self, company_id: int, since: dt.datetime, *, user_id: int | None = None, feature: str | None = None
    ) -> Totals:
        t = Totals()
        for e in self.entries:
            if e.company_id != company_id or e.ts < since or e.status not in BILLABLE:
                continue
            if user_id is not None and e.user_id != user_id:
                continue
            if feature is not None and e.feature != feature:
                continue
            t.cost += e.cost
            t.tokens += e.tokens
            t.requests += 1
        return t

    async def report(
        self, *, company_id: int | None, since: dt.datetime | None, until: dt.datetime | None, group_by: list[str]
    ) -> list[dict[str, Any]]:
        rows = [
            e
            for e in self.entries
            if (company_id is None or e.company_id == company_id)
            and (since is None or e.ts >= since)
            and (until is None or e.ts < until)
        ]
        return _aggregate(rows, group_by)


DDL = """
CREATE SCHEMA IF NOT EXISTS ai_gateway;
CREATE TABLE IF NOT EXISTS ai_gateway.ledger (
    id bigserial PRIMARY KEY,
    ts timestamptz NOT NULL DEFAULT now(),
    company_id bigint NOT NULL,
    user_id bigint,
    feature text NOT NULL DEFAULT '',
    prompt_version text NOT NULL DEFAULT '',
    run_id text NOT NULL DEFAULT '',
    task_class text NOT NULL DEFAULT '',
    kind text NOT NULL DEFAULT 'complete',
    provider text NOT NULL DEFAULT '',
    model text NOT NULL DEFAULT '',
    input_tokens integer NOT NULL DEFAULT 0,
    output_tokens integer NOT NULL DEFAULT 0,
    cache_read_tokens integer NOT NULL DEFAULT 0,
    cache_write_tokens integer NOT NULL DEFAULT 0,
    cost numeric(14,6) NOT NULL DEFAULT 0,
    latency_ms integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'ok',
    fallback boolean NOT NULL DEFAULT false,
    cached boolean NOT NULL DEFAULT false,
    redacted boolean NOT NULL DEFAULT false,
    inference_geo text,
    retention text NOT NULL DEFAULT '',
    error text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ledger_company_ts_idx ON ai_gateway.ledger (company_id, ts);
CREATE INDEX IF NOT EXISTS ledger_run_idx ON ai_gateway.ledger (run_id);
"""

COLUMNS = [
    "ts", "company_id", "user_id", "feature", "prompt_version", "run_id", "task_class", "kind", "provider", "model",
    "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost", "latency_ms", "status",
    "fallback", "cached", "redacted", "inference_geo", "retention", "error",
]  # fmt: skip


class SqlLedger:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self._pool: Any = None

    async def _get_pool(self) -> Any:
        if self._pool is None:
            from psycopg_pool import AsyncConnectionPool

            pool = AsyncConnectionPool(
                self.database_url, min_size=1, max_size=8, open=False, kwargs={"autocommit": True}
            )
            await pool.open()
            async with pool.connection() as conn:
                await conn.execute(DDL)
            self._pool = pool
        return self._pool

    async def record(self, entry: Entry) -> None:
        pool = await self._get_pool()
        values = asdict(entry)
        placeholders = ", ".join(f"%({c})s" for c in COLUMNS)
        async with pool.connection() as conn:
            await conn.execute(f"INSERT INTO ai_gateway.ledger ({', '.join(COLUMNS)}) VALUES ({placeholders})", values)

    async def totals(
        self, company_id: int, since: dt.datetime, *, user_id: int | None = None, feature: str | None = None
    ) -> Totals:
        pool = await self._get_pool()
        where = ["company_id = %(c)s", "ts >= %(since)s", "status = ANY(%(billable)s)"]
        params: dict[str, Any] = {"c": company_id, "since": since, "billable": list(BILLABLE)}
        if user_id is not None:
            where.append("user_id = %(u)s")
            params["u"] = user_id
        if feature is not None:
            where.append("feature = %(f)s")
            params["f"] = feature
        sql = (
            "SELECT coalesce(sum(cost), 0), coalesce(sum(input_tokens + output_tokens + cache_read_tokens"
            f" + cache_write_tokens), 0), count(*) FROM ai_gateway.ledger WHERE {' AND '.join(where)}"
        )
        async with pool.connection() as conn:
            cur = await conn.execute(sql, params)
            cost, tokens, count = await cur.fetchone()
        return Totals(Decimal(cost), int(tokens), int(count))

    async def report(
        self, *, company_id: int | None, since: dt.datetime | None, until: dt.datetime | None, group_by: list[str]
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        where: list[str] = ["true"]
        params: dict[str, Any] = {}
        if company_id is not None:
            where.append("company_id = %(c)s")
            params["c"] = company_id
        if since is not None:
            where.append("ts >= %(since)s")
            params["since"] = since
        if until is not None:
            where.append("ts < %(until)s")
            params["until"] = until
        async with pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {', '.join(COLUMNS)} FROM ai_gateway.ledger WHERE {' AND '.join(where)}", params
            )
            rows = await cur.fetchall()
        entries = [Entry(**dict(zip(COLUMNS, r, strict=True))) for r in rows]
        return _aggregate(entries, group_by)

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
