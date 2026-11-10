"""A registry rebuild (Studio) never exposes a half-built registry to requests running meanwhile."""

from __future__ import annotations

from typing import Any


def test_rebuild_composes_on_a_staging_copy_and_publishes_at_once(registry: Any) -> None:
    seen: list[tuple[int, bool, bool]] = []

    def observe(staging: Any) -> None:
        # called mid-build: the live registry still serves the previous, complete models
        seen.append((len(registry.models), "core.user" in registry.tables, staging is registry))

    before = registry.models["core.user"]
    registry.hooks["before_build"].append(observe)
    try:
        registry.build()
    finally:
        registry.hooks["before_build"].remove(observe)
    assert seen and seen[0][0] > 0 and seen[0][1] and not seen[0][2]
    rebuilt = registry.models["core.user"]
    assert rebuilt is not before and rebuilt._registry is registry
    assert registry.tables["core.user"] is not None and registry.ready


def test_a_process_started_before_install_picks_up_modules_installed_elsewhere(registry: Any) -> None:
    """U-1: the API starts on an empty database, then `tessera seed demo` installs modules from another
    process; the next request loads them instead of answering "Unknown model"."""
    import uuid

    import psycopg

    from tessera.config import get_settings
    from tessera.db.engine import Cursor, get_engine
    from tessera.modules.installer import drop_everything, install_modules
    from tessera.orm.registry import Registry, current_registry

    base = get_settings().test_database_url.rsplit("/", 1)[0]
    name = f"reload_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(base + "/postgres", autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = f"{base}/{name}"
    try:
        stale = Registry.get(url)  # the API process, started on the empty database
        assert not stale.ready and stale.stamp == 0
        install_modules(["core"], database_url=url)  # "another process"
        Registry._registries[stale.db_url] = stale  # this process still holds its old registry
        cr = Cursor(get_engine(url))
        try:
            fresh = current_registry(url, cr)
            assert fresh is not stale and fresh.ready and "core.user" in fresh.models
            assert current_registry(url, cr) is fresh  # loaded once
        finally:
            cr.close()
    finally:
        Registry.forget(url)
        drop_everything(database_url=url)
        with psycopg.connect(base + "/postgres", autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
