"""PostgreSQL-backed configuration store and ledger (the deployed mode)."""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator
from decimal import Decimal

import psycopg
import pytest

from gateway.config import SqlConfigStore
from gateway.crypto import decrypt, encrypt
from gateway.ledger import Entry, SqlLedger

pytestmark = pytest.mark.db

ADMIN_URL = os.environ.get("GATEWAY_TEST_ADMIN_URL", "postgresql://tessera@127.0.0.1:5433/postgres")
MASTER = "ZGV2LW9ubHktbWFzdGVyLWtleS1jaGFuZ2UtbWUtMzItYnl0ZXMh"

AI_CORE_DDL = """
CREATE TABLE ai_core_provider (id serial primary key, name text, kind text, api_key text, base_url text, region text,
    approved_for_pii boolean, retention text, options jsonb, active boolean default true);
CREATE TABLE ai_core_route (id serial primary key, company_id bigint, task_class text, provider_id bigint, model text,
    params jsonb, fallback_provider_id bigint, fallback_model text, fallback_params jsonb, active boolean default true);
CREATE TABLE ai_core_budget (id serial primary key, company_id bigint, user_id bigint, feature text, period text,
    limit_cost numeric, limit_tokens bigint, limit_requests bigint, hard boolean, alert_pct bigint, active boolean default true);
CREATE TABLE ai_core_feature (id serial primary key, name text);
CREATE TABLE ai_core_feature_flag (id serial primary key, feature_id bigint, company_id bigint, enabled boolean);
CREATE TABLE ai_core_company_setting (id serial primary key, company_id bigint, ai_enabled boolean, allowed_regions jsonb);
"""


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    name = f"gateway_test_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg.connect(ADMIN_URL, autocommit=True)
    except psycopg.OperationalError:
        pytest.skip("PostgreSQL not reachable (GATEWAY_TEST_ADMIN_URL)")
    admin.execute(f'CREATE DATABASE "{name}"')
    url = ADMIN_URL.rsplit("/", 1)[0] + f"/{name}"
    try:
        yield url
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def test_sql_config_store_reads_ai_core_tables(database_url: str) -> None:
    store = SqlConfigStore(database_url, master_key=MASTER, ttl=0, env_keys={"openai": "env-openai-key"})
    assert store.get().providers == {}  # tables not installed yet → empty configuration
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(AI_CORE_DDL)
        conn.execute(
            "INSERT INTO ai_core_provider (name, kind, api_key, region, approved_for_pii, retention, options) VALUES "
            "('Claude', 'anthropic', %s, 'us', false, '30 days', '{\"inference_geo\": \"us\"}'),"
            "('OpenAI', 'openai', NULL, 'us', false, '', '{}'),"
            "('Off', 'mock', NULL, 'eu', true, '', '{}')",
            (encrypt("sk-ant-secret", MASTER),),
        )
        conn.execute("UPDATE ai_core_provider SET active = false WHERE name = 'Off'")
        conn.execute(
            "INSERT INTO ai_core_route (company_id, task_class, provider_id, model, params, fallback_provider_id, fallback_model) "
            "VALUES (NULL, 'draft', 1, 'claude-sonnet-5-5', '{\"effort\": \"medium\"}', 2, 'gpt-x'), (2, 'draft', 2, 'gpt-x', NULL, NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO ai_core_budget (company_id, period, limit_cost, hard, alert_pct) VALUES (1, 'month', 12.5, true, 80)"
        )
        conn.execute("INSERT INTO ai_core_feature (name) VALUES ('discuss.reply_draft')")
        conn.execute(
            "INSERT INTO ai_core_feature_flag (feature_id, company_id, enabled) VALUES (1, 2, false), (1, NULL, true)"
        )
        conn.execute(
            "INSERT INTO ai_core_company_setting (company_id, ai_enabled, allowed_regions) VALUES (3, false, '[]'), (1, true, '[\"us\"]')"
        )
    store.invalidate()
    cfg = store.get()
    assert {p.name for p in cfg.providers.values()} == {"Claude", "OpenAI"}
    claude = next(p for p in cfg.providers.values() if p.kind == "anthropic")
    assert claude.api_key == "sk-ant-secret" and claude.options["inference_geo"] == "us"
    assert next(p for p in cfg.providers.values() if p.kind == "openai").api_key == "env-openai-key"
    route = cfg.route_for(1, "draft")
    assert route and route.primary.model == "claude-sonnet-5-5" and route.primary.params == {"effort": "medium"}
    assert route.fallback and route.fallback.model == "gpt-x"
    assert cfg.route_for(2, "draft").primary.model == "gpt-x"  # type: ignore[union-attr]
    assert cfg.budgets[0].limit_cost == Decimal("12.5")
    assert cfg.feature_enabled("discuss.reply_draft", 1) and not cfg.feature_enabled("discuss.reply_draft", 2)
    assert not cfg.feature_enabled("anything", 3)
    assert cfg.companies[1].allowed_regions == ["us"]


def test_wrong_master_key_fails_closed() -> None:
    token = encrypt("secret", MASTER)
    assert decrypt(token, MASTER) == "secret"
    other = "b3RoZXItbWFzdGVyLWtleS0zMi1ieXRlcy1sb25nLWVub3VnaA=="
    assert decrypt(token, other) is None
    assert decrypt("plain", MASTER) == "plain" and decrypt(None, MASTER) is None


async def test_sql_ledger_totals_and_report(database_url: str) -> None:
    ledger = SqlLedger(database_url)
    try:
        now = dt.datetime.now(dt.UTC)
        await ledger.record(
            Entry(
                1,
                "crm.a",
                "classify",
                "complete",
                "mock",
                "mock-small",
                user_id=4,
                input_tokens=100,
                output_tokens=10,
                cost=Decimal("0.000120"),
                latency_ms=40,
            )
        )
        await ledger.record(
            Entry(
                1,
                "crm.a",
                "classify",
                "complete",
                "mock",
                "mock-small",
                user_id=5,
                input_tokens=50,
                output_tokens=5,
                cost=Decimal("0.000060"),
                latency_ms=80,
                fallback=True,
            )
        )
        await ledger.record(Entry(1, "crm.a", "classify", "complete", status="budget_exceeded"))
        await ledger.record(
            Entry(2, "crm.b", "draft", "stream", "mock", "mock-mid", input_tokens=10, cost=Decimal("0.000030"))
        )
        totals = await ledger.totals(1, now - dt.timedelta(hours=1))
        assert totals.requests == 2 and totals.cost == Decimal("0.000180") and totals.tokens == 165
        assert (await ledger.totals(1, now - dt.timedelta(hours=1), user_id=5)).requests == 1
        assert (await ledger.totals(1, now - dt.timedelta(hours=1), feature="crm.b")).requests == 0
        rows = await ledger.report(
            company_id=1, since=now - dt.timedelta(hours=1), until=None, group_by=["feature", "model"]
        )
        row = next(r for r in rows if r["model"] == "mock-small")
        assert Decimal(row["cost"]) == Decimal("0.000180") and row["requests"] == 2 and row["fallback_rate"] == 0.5
        assert row["latency_p50"] == 60.0 and row["latency_p95"] == 80.0
        blocked = next(r for r in rows if r["model"] == "")
        assert blocked["blocked"] == 1 and blocked["requests"] == 0
        assert (
            len(await ledger.report(company_id=None, since=None, until=now - dt.timedelta(days=1), group_by=["day"]))
            == 0
        )
    finally:
        await ledger.close()
