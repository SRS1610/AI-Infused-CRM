"""Background jobs: execution context, retries with backoff, dead letter, requeue, idempotency and the
scheduler's exactly-once guarantee with concurrent ticks (1.3-e, 1.3-f)."""

from __future__ import annotations

import datetime as dt
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from tessera.jobs import enqueue, job, run_now
from tessera.jobs.registry import JobSpec
from tessera.jobs.scheduler import next_run, tick
from tessera.orm.environment import ADMIN_UID, SYSTEM_UID, environment_for

pytestmark = pytest.mark.db

CALLS: list[dict[str, Any]] = []
FAILURES: dict[str, int] = {}


@job("test.record_call")
def record_call(env: Any, **kwargs: Any) -> dict[str, Any]:
    CALLS.append({"uid": env.uid, "su": env.su, "kwargs": kwargs, "company": env.company.ids})
    return {"ok": True}


@job("test.flaky", max_attempts=3, backoff_seconds=10)
def flaky(env: Any, key: str, fail_times: int) -> str:
    FAILURES[key] = FAILURES.get(key, 0) + 1
    if FAILURES[key] <= fail_times:
        raise RuntimeError(f"boom {FAILURES[key]}")
    return "recovered"


@job("test.writes_then_fails", max_attempts=1)
def writes_then_fails(env: Any, name: str) -> None:
    env["ktest.tag"].create({"name": name})
    raise RuntimeError("after write")


@job("test.system", as_system=True)
def system_job(env: Any) -> None:
    CALLS.append({"uid": env.uid, "su": env.su})


@pytest.fixture
def jobs_db(registry: Any, database_url: str) -> Iterator[str]:
    CALLS.clear()
    FAILURES.clear()
    yield database_url
    with environment_for(database_url, SYSTEM_UID, su=True) as env:
        env.cr.execute("DELETE FROM core_job WHERE name LIKE 'test.%' OR idempotency_key LIKE 'sched:%'")
        env.cr.execute("DELETE FROM core_scheduled_job WHERE job_name LIKE 'test.%'")
        env.cr.execute("DELETE FROM ktest_tag WHERE name LIKE 'job-%'")


def _enqueue(db: str, name: str, kwargs: dict[str, Any] | None = None, uid: int = ADMIN_UID, **kw: Any) -> int:
    with environment_for(db, uid) as env:
        return enqueue(env, name, kwargs, **kw).ids[0]


def _job(db: str, job_id: int) -> dict[str, Any]:
    with environment_for(db, SYSTEM_UID, su=True) as env:
        rec = env["core.job"].browse([job_id])
        attempts = [(a.number, a.status) for a in rec.attempt_ids]
        return {
            "state": rec.state,
            "attempts": rec.attempts,
            "error": rec.error,
            "result": rec.result,
            "next": rec.next_attempt_at,
            "history": attempts,
        }


def test_backoff_is_exponential_and_capped() -> None:
    spec = JobSpec(name="x", func=lambda env: None, backoff_seconds=30)
    assert [spec.delay_for(n) for n in (1, 2, 3)] == [30, 60, 120]
    assert spec.delay_for(20) == 3600


def test_job_runs_as_enqueuing_user_with_kwargs(jobs_db: str) -> None:
    jid = _enqueue(jobs_db, "test.record_call", {"a": 1})
    assert _job(jobs_db, jid)["state"] == "queued"  # manual backend: nothing ran yet
    assert run_now(jobs_db, [jid]) == {jid: "done"}
    assert CALLS == [{"uid": ADMIN_UID, "su": False, "kwargs": {"a": 1}, "company": [1]}]
    info = _job(jobs_db, jid)
    assert info["state"] == "done" and info["result"] == {"ok": True} and info["history"] == [(1, "done")]


def test_as_system_job_runs_elevated(jobs_db: str) -> None:
    jid = _enqueue(jobs_db, "test.system")
    run_now(jobs_db, [jid])
    assert CALLS == [{"uid": SYSTEM_UID, "su": True}]


def test_retry_then_recover(jobs_db: str) -> None:
    jid = _enqueue(jobs_db, "test.flaky", {"key": "r", "fail_times": 1})
    assert run_now(jobs_db, [jid]) == {jid: "retry"}
    info = _job(jobs_db, jid)
    assert info["state"] == "queued" and "boom 1" in info["error"]
    assert info["next"] > dt.datetime.now(dt.UTC) + dt.timedelta(seconds=5)  # backoff applied
    assert jid not in run_now(jobs_db, due_only=True)  # not due yet
    assert run_now(jobs_db, [jid]) == {jid: "done"}
    assert _job(jobs_db, jid)["history"] == [(1, "failed"), (2, "done")]


def test_dead_letter_after_max_attempts_and_requeue(jobs_db: str) -> None:
    jid = _enqueue(jobs_db, "test.flaky", {"key": "d", "fail_times": 3})
    states = [run_now(jobs_db, [jid])[jid] for _ in range(3)]
    assert states == ["retry", "retry", "dead"]
    assert run_now(jobs_db, [jid]) == {jid: "dead"}  # dead jobs are not picked up again
    with environment_for(jobs_db, ADMIN_UID) as env:
        env["core.job"].browse([jid]).action_requeue()
    assert run_now(jobs_db, [jid]) == {jid: "done"}
    assert _job(jobs_db, jid)["attempts"] == 4


def test_failed_job_rolls_back_its_writes(jobs_db: str) -> None:
    jid = _enqueue(jobs_db, "test.writes_then_fails", {"name": "job-rollback"})
    assert run_now(jobs_db, [jid]) == {jid: "dead"}
    with environment_for(jobs_db, SYSTEM_UID, su=True) as env:
        assert not env["ktest.tag"].search([("name", "=", "job-rollback")])


def test_idempotency_key_returns_existing_job(jobs_db: str) -> None:
    a = _enqueue(jobs_db, "test.record_call", idempotency_key="test-key-1")
    b = _enqueue(jobs_db, "test.record_call", idempotency_key="test-key-1")
    assert a == b


def test_cancel_only_queued(jobs_db: str) -> None:
    from tessera.exceptions import UserError

    jid = _enqueue(jobs_db, "test.record_call")
    with environment_for(jobs_db, ADMIN_UID) as env:
        env["core.job"].browse([jid]).action_cancel()
    assert run_now(jobs_db, [jid]) == {jid: "cancelled"}
    with pytest.raises(UserError), environment_for(jobs_db, ADMIN_UID) as env:
        env["core.job"].browse([jid]).action_cancel()


def test_users_only_see_their_own_jobs(jobs_db: str) -> None:
    with environment_for(jobs_db, ADMIN_UID) as env:
        demo = env["core.user"].search([("login", "=", "demo")]).ids[0]
    mine = _enqueue(jobs_db, "test.record_call", uid=demo)
    other = _enqueue(jobs_db, "test.record_call", uid=ADMIN_UID)
    with environment_for(jobs_db, demo) as env:
        visible = env["core.job"].search([("name", "=", "test.record_call")]).ids
    assert mine in visible and other not in visible


def test_next_run_cron_and_interval() -> None:
    base = dt.datetime(2026, 1, 1, 10, 7, tzinfo=dt.UTC)
    assert next_run("*/15 * * * *", None, base) == dt.datetime(2026, 1, 1, 10, 15, tzinfo=dt.UTC)
    assert next_run(None, 90, base) == base + dt.timedelta(seconds=90)


def test_scheduled_job_validation(env: Any) -> None:
    from tessera.exceptions import ValidationError

    with pytest.raises(ValidationError):
        env["core.scheduled_job"].create({"name": "bad", "job_name": "test.record_call", "cron": "not a cron"})
    with pytest.raises(ValidationError):
        env["core.scheduled_job"].create({"name": "bad", "job_name": "test.record_call"})
    with pytest.raises(ValidationError):
        env["core.scheduled_job"].create({"name": "bad", "job_name": "no.such.job", "interval_seconds": 60})


def test_concurrent_scheduler_ticks_enqueue_exactly_once(jobs_db: str) -> None:
    past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    with environment_for(jobs_db, ADMIN_UID) as env:
        sched = env["core.scheduled_job"].create(
            {
                "name": "every minute",
                "job_name": "test.record_call",
                "cron": "* * * * *",
                "next_run": past,
                "kwargs": {"s": 1},
            }
        )
        sid = sched.ids[0]

    results: list[list[int]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(tick(jobs_db))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors
    created = [j for r in results for j in r]
    assert len(created) == len(set(created))  # no job claimed by two ticks
    with environment_for(jobs_db, SYSTEM_UID, su=True) as env:
        jobs = env["core.job"].search([("idempotency_key", "like", f"sched:{sid}:%")])
        sched = env["core.scheduled_job"].browse([sid])
        assert len(jobs) == 1 and jobs.ids[0] in created
        assert sched.next_run > dt.datetime.now(dt.UTC) and sched.last_job_id.ids == jobs.ids
        mine = jobs.ids
    # another tick does not enqueue it again before the next due time
    tick(jobs_db)
    with environment_for(jobs_db, SYSTEM_UID, su=True) as env:
        assert env["core.job"].search_count([("idempotency_key", "like", f"sched:{sid}:%")]) == 1
    run_now(jobs_db, mine)
    assert CALLS[0]["kwargs"] == {"s": 1} and CALLS[0]["uid"] == SYSTEM_UID


def test_scheduled_job_run_now_button(jobs_db: str) -> None:
    with environment_for(jobs_db, ADMIN_UID) as env:
        sched = env["core.scheduled_job"].create(
            {"name": "hourly", "job_name": "test.record_call", "interval_seconds": 3600}
        )
        jid = sched.action_run_now()["job_id"]
    assert run_now(jobs_db, [jid]) == {jid: "done"}
