"""Shared pytest fixtures.

Database tests (``@pytest.mark.db``) run against ``TESSERA_TEST_DATABASE_URL``. The schema is
built once per session (``core`` + the kernel test modules + demo data); each test runs in a
transaction that is rolled back, so tests never see each other's data.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parent
TEST_MODULE_PATH = BACKEND / "tessera" / "testing" / "modules"

os.environ.setdefault("TESSERA_ENV", "test")
os.environ["TESSERA_MODULE_PATHS"] = f"{BACKEND / 'modules'}:{TEST_MODULE_PATH}"
os.environ.setdefault("TESSERA_STORAGE_BACKEND", "filesystem")
os.environ["TESSERA_JOBS_BACKEND"] = "manual"
os.environ.setdefault("TESSERA_STORAGE_PATH", str(BACKEND / "var" / "test-storage"))

from tessera.config import get_settings, reset_settings_cache

reset_settings_cache()
_settings = get_settings()
os.environ["TESSERA_DATABASE_URL"] = _settings.test_database_url
reset_settings_cache()

KERNEL_TEST_MODULES = [
    "core",
    "web",
    "kernel_test_a",
    "kernel_test_b",
    "chatter",
    "chatter_test",
    "workflow",
    "reports",
    "contacts",
    "contacts_test",
    "product",
    "sale",
]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "db: needs the test database")


def _db_available() -> bool:
    try:
        from tessera.db.engine import get_engine

        with get_engine(get_settings().test_database_url).connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def database_url() -> str:
    return get_settings().test_database_url


@pytest.fixture(scope="session")
def registry(database_url: str):  # type: ignore[no-untyped-def]
    """Fresh schema with core + kernel test modules + demo data, built once per session."""
    if not _db_available():
        pytest.skip("test database not reachable (TESSERA_TEST_DATABASE_URL)")
    from tessera.modules.installer import drop_everything, install_modules
    from tessera.orm.registry import Registry
    from tessera.storage import FilesystemStorage, reset_storage

    if os.environ.get("TESSERA_KEEP_TEST_DB") != "1":
        drop_everything(database_url=database_url)
        storage = FilesystemStorage(Path(_settings.storage_path) / "tessera_test")
        storage.wipe()
    reset_storage()
    install_modules(KERNEL_TEST_MODULES, database_url=database_url, demo=True)
    return Registry.get(database_url)


@pytest.fixture
def cr(registry, database_url: str):  # type: ignore[no-untyped-def]
    """A transaction that is always rolled back."""
    from tessera.db.engine import Cursor, get_engine

    cursor = Cursor(get_engine(database_url))
    try:
        yield cursor
    finally:
        cursor.rollback()
        cursor.close()


@pytest.fixture
def env(registry, cr):  # type: ignore[no-untyped-def]
    """Environment of the ``admin`` user (full rights through ``core.group_system``, no bypass)."""
    from tessera.orm.environment import ADMIN_UID, Environment

    return Environment.new(registry, cr, ADMIN_UID)


@pytest.fixture
def system_env(registry, cr):  # type: ignore[no-untyped-def]
    from tessera.orm.environment import SYSTEM_UID, Environment

    return Environment.new(registry, cr, SYSTEM_UID, su=True)


@pytest.fixture
def env_for(env) -> Callable[[str], object]:  # type: ignore[no-untyped-def]
    """``env_for("demo")`` → environment of the user with that login."""

    def factory(login: str):  # type: ignore[no-untyped-def]
        user = env["core.user"].elevated().with_context(active_test=False).search([("login", "=", login)], limit=1)
        assert user, f"no user with login {login}"
        return env.with_user(user.ids[0])

    return factory


@pytest.fixture
def commit_env(registry, database_url: str) -> Iterator[object]:
    """An environment whose transaction is committed (for tests that need visibility across connections)."""
    from tessera.orm.environment import environment_for

    with environment_for(database_url, uid=1, su=True) as e:
        yield e
