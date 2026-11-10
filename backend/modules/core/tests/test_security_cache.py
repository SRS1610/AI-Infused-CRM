"""The security cache never serves rights from rolled-back work or misses changes committed elsewhere."""

from __future__ import annotations

from typing import Any

import pytest

from tessera.db.engine import Cursor, get_engine
from tessera.orm.environment import SYSTEM_UID, Environment, environment_for

pytestmark = pytest.mark.db


def _reader_sees_drafts(registry: Any, url: str) -> bool:
    with environment_for(url, SYSTEM_UID, su=True) as sys_env:
        reader = sys_env["core.user"].search([("login", "=", "reader")]).ids[0]
        draft = sys_env.ref("kernel_test_a.book_draft").ids[0]
    with environment_for(url, reader) as env:
        return bool(env["ktest.book"].search([("id", "=", draft)]))


def test_rolled_back_group_grant_does_not_leak(registry: Any, database_url: str) -> None:
    assert not _reader_sees_drafts(registry, database_url)
    cr = Cursor(get_engine(database_url))
    try:
        env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        reader = env["core.user"].search([("login", "=", "reader")])
        reader.write({"group_ids": [("add", env.ref("kernel_test_a.group_librarian").ids[0])]})
        draft = env.ref("kernel_test_a.book_draft").ids[0]
        as_reader = Environment(env.transaction, reader.ids[0], {})
        assert as_reader["ktest.book"].search([("id", "=", draft)])  # visible inside the transaction
    finally:
        cr.rollback()
        cr.close()
    assert not _reader_sees_drafts(registry, database_url)  # gone after rollback


def test_change_committed_by_another_connection_is_seen(registry: Any, database_url: str) -> None:
    assert not _reader_sees_drafts(registry, database_url)
    # simulate another worker: change memberships with raw SQL + stamp bump, bypassing this process's hooks
    librarian_group = None
    with environment_for(database_url, SYSTEM_UID, su=True) as env:
        librarian_group = env.ref("kernel_test_a.group_librarian").ids[0]
        reader = env["core.user"].search([("login", "=", "reader")]).ids[0]
        env.cr.execute("INSERT INTO core_user__group_ids VALUES (:u, :g)", {"u": reader, "g": librarian_group})
        env.cr.execute("UPDATE core_security_stamp SET version = version + 7 WHERE id = 1")
    try:
        assert _reader_sees_drafts(registry, database_url)
    finally:
        with environment_for(database_url, SYSTEM_UID, su=True) as env:
            env.cr.execute(
                "DELETE FROM core_user__group_ids WHERE core_user_id = :u AND core_group_id = :g",
                {"u": reader, "g": librarian_group},
            )
            env.cr.execute("UPDATE core_security_stamp SET version = version + 7 WHERE id = 1")
    assert not _reader_sees_drafts(registry, database_url)
