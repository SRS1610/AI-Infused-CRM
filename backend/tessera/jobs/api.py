"""Enqueue, dispatch and execute jobs."""

from __future__ import annotations

import datetime as dt
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any

from tessera.config import get_settings
from tessera.db.engine import Cursor, get_engine
from tessera.jobs.registry import get_job
from tessera.logging import bind_context, get_logger

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

log = get_logger(__name__)


def enqueue(
    env: Environment,
    name: str,
    kwargs: dict[str, Any] | None = None,
    *,
    delay_seconds: int = 0,
    idempotency_key: str | None = None,
    max_attempts: int | None = None,
    queue: str | None = None,
    description: str | None = None,
) -> Model:
    """Create a ``core.job`` row and dispatch it after commit. With an ``idempotency_key`` an existing
    job with the same key is returned instead of creating a duplicate."""
    spec = get_job(name)
    jobs = env["core.job"].elevated("enqueue")
    if idempotency_key:
        existing = jobs.search([("idempotency_key", "=", idempotency_key)], limit=1)
        if existing:
            return existing
    run_at = env.now() + dt.timedelta(seconds=delay_seconds)
    record = jobs.create(
        {
            "name": name,
            "description": description or name,
            "kwargs": kwargs or {},
            "queue": queue or spec.queue,
            "max_attempts": max_attempts or spec.max_attempts,
            "state": "queued",
            "next_attempt_at": run_at,
            "idempotency_key": idempotency_key,
            "user_id": env.uid if env.uid > 0 else None,
            "company_id": env.company.ids[0] if env.company else None,
        }
    )
    db_url = env.registry.db_url
    job_id = record.ids[0]
    env.cr.after_commit(lambda: dispatch(db_url, job_id, delay_seconds))
    return record


def dispatch(db_url: str, job_id: int, delay_seconds: int = 0) -> None:
    backend = get_settings().jobs_backend
    if backend == "celery":
        from tessera.jobs.celery_app import celery_app

        queue = _job_queue(db_url, job_id)
        celery_app.send_task("tessera.run_job", args=[db_url, job_id], queue=queue, countdown=max(delay_seconds, 0))
    elif backend == "inline":
        timer = threading.Timer(max(delay_seconds, 0), process_job, args=(db_url, job_id))
        timer.daemon = True
        timer.start()
    # "manual": jobs wait for run_now()/the sweeper (used by tests to control execution)


def _job_queue(db_url: str, job_id: int) -> str:
    cr = Cursor(get_engine(db_url))
    try:
        return str(cr.scalar("SELECT queue FROM core_job WHERE id = :id", {"id": job_id}) or "default")
    finally:
        cr.close()


def process_job(db_url: str, job_id: int) -> str:
    """Run one job in its own transactions. Returns the final state of this attempt."""
    from tessera.orm.environment import SYSTEM_UID, Environment
    from tessera.orm.registry import current_registry

    engine = get_engine(db_url)

    # 1. claim: queued → running (row lock prevents two workers running the same job)
    cr = Cursor(engine)
    registry = current_registry(db_url, cr)
    try:
        row = cr.fetchone(
            "SELECT state, attempts, user_id, company_id FROM core_job WHERE id = :id FOR UPDATE SKIP LOCKED",
            {"id": job_id},
        )
        if row is None or row[0] != "queued":
            cr.rollback()
            return row[0] if row else "missing"
        attempt = int(row[1] or 0) + 1
        cr.execute(
            "UPDATE core_job SET state = 'running', attempts = :n, started_at = now() WHERE id = :id",
            {"n": attempt, "id": job_id},
        )
        cr.execute(
            "INSERT INTO core_job_attempt (job_id, number, status, started_at) VALUES (:id, :n, 'running', now())",
            {"id": job_id, "n": attempt},
        )
        cr.commit()
        uid, company_id = row[2], row[3]
    finally:
        cr.close()

    # 2. execute in a fresh transaction as the enqueuing user
    cr = Cursor(engine)
    error: str | None = None
    result: Any = None
    job_name = "unknown"
    run_started = time.perf_counter()
    try:
        sys_env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        record = sys_env["core.job"].browse([job_id])
        job_name = record.name
        spec = get_job(record.name)
        bind_context(job=record.name, job_id=job_id)
        if spec.as_system or not uid:
            env = sys_env
        else:
            ctx = {"allowed_company_ids": [company_id]} if company_id else {}
            if company_id:
                ctx["company_id"] = company_id
            env = Environment(sys_env.transaction, uid, ctx)
        result = spec.func(env, **(record.kwargs or {}))
        env.flush()
        cr.commit()
    except Exception as exc:
        cr.rollback()
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=8)}"
        log.warning("job failed", job_id=job_id, error=str(exc)[:300])
    finally:
        cr.close()
    run_seconds = time.perf_counter() - run_started

    # 3. record outcome
    cr = Cursor(engine)
    try:
        sys_env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        record = sys_env["core.job"].browse([job_id])
        attempt_row = sys_env["core.job_attempt"].search([("job_id", "=", job_id)], order="number desc", limit=1)
        now = sys_env.now()
        if error is None:
            record.write(
                {"state": "done", "finished_at": now, "result": _jsonable(result), "error": None, "progress": 100}
            )
            attempt_row.write({"status": "done", "finished_at": now})
            state = "done"
        else:
            spec = get_job(record.name)
            attempt_row.write({"status": "failed", "finished_at": now, "error": error[:4000]})
            if record.attempts >= record.max_attempts:
                record.write({"state": "dead", "finished_at": now, "error": error[:4000]})
                state = "dead"
            else:
                delay = spec.delay_for(record.attempts)
                record.write(
                    {"state": "queued", "error": error[:4000], "next_attempt_at": now + dt.timedelta(seconds=delay)}
                )
                cr.after_commit(lambda: dispatch(db_url, job_id, delay))
                state = "retry"
        cr.commit()
        from tessera.metrics import JOB_SECONDS, JOBS

        JOBS.labels(job_name, state).inc()
        JOB_SECONDS.labels(job_name).observe(run_seconds)
        return state
    finally:
        cr.close()


def run_now(db_url: str, job_ids: list[int] | None = None, *, due_only: bool = False) -> dict[int, str]:
    """Run queued jobs synchronously (tests, ``tessera jobs run``, the sweeper in manual mode)."""
    cr = Cursor(get_engine(db_url))
    try:
        if job_ids is None:
            sql = "SELECT id FROM core_job WHERE state = 'queued'"
            if due_only:
                sql += " AND (next_attempt_at IS NULL OR next_attempt_at <= now())"
            job_ids = [r[0] for r in cr.fetchall(sql + " ORDER BY id")]
    finally:
        cr.close()
    return {jid: process_job(db_url, jid) for jid in job_ids}


def _jsonable(value: Any) -> Any:
    """Job results are stored as JSON objects; scalars are wrapped as ``{"value": …}``."""
    if value is None or isinstance(value, dict | list):
        return value
    if hasattr(value, "_ids"):
        return {"model": value._name, "ids": list(value._ids)}
    if isinstance(value, int | float | bool | str):
        return {"value": value}
    return {"value": str(value)}
