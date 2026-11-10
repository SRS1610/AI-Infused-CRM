"""1.4-a: installing crm on an empty database pulls its dependencies in < 60 s with no schema drift."""

from __future__ import annotations

import time
from typing import Any

import pytest
from sqlalchemy.engine import make_url

from tessera.db.engine import Cursor, get_engine

pytestmark = [pytest.mark.db, pytest.mark.slow]


def test_install_crm_on_empty_database(database_url: str, registry: Any) -> None:
    from tessera.modules.installer import check_schema, install_modules

    url = make_url(database_url)
    name = f"{url.database}_install_check"

    def admin_sql(sql: str) -> None:
        import psycopg

        dsn = url.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql)  # type: ignore[arg-type]

    admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    admin_sql(f'CREATE DATABASE "{name}"')
    target = url.set(database=name).render_as_string(hide_password=False)
    try:
        started = time.perf_counter()
        result = install_modules(["crm"], database_url=target)
        elapsed = time.perf_counter() - started
        cr = Cursor(get_engine(target))
        try:
            installed = {r[0] for r in cr.fetchall("SELECT name FROM core_module WHERE state = 'installed'")}
        finally:
            cr.close()
        assert {
            "core",
            "web",
            "contacts",
            "chatter",
            "calendar",
            "product",
            "sale",
            "workflow",
            "reports",
            "crm",
        } <= installed
        assert "kernel_test_a" not in installed  # nothing outside the dependency closure (+ auto-installs)
        assert elapsed < 60, f"install took {elapsed:.1f}s"
        assert check_schema(database_url=target) == []
        assert result is not None
    finally:
        get_engine(target).dispose()
        admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
