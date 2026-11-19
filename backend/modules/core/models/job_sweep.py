"""Housekeeping jobs for the job system itself."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.jobs import job
from tessera.jobs.api import dispatch
from tessera.jobs.registry import get_job, registered_jobs


@job("core.jobs_sweep", queue="default", max_attempts=1, as_system=True)
def sweep_jobs(env: Any, stale_after_seconds: int = 300, keep_days: int = 30) -> dict[str, int]:
    """Recover from lost workers and lost dispatches, and prune old finished jobs.

    * ``running`` longer than the job's timeout → attempt marked failed; re-queued or dead-lettered.
    * ``queued`` and due for more than ``stale_after_seconds`` → dispatched again (a broker message was lost).
    * ``done``/``cancelled`` older than ``keep_days`` → deleted (dead jobs are kept for inspection).
    """
    now = env.now()
    jobs = env["core.job"]
    stuck = requeued = redispatched = 0
    for rec in jobs.search([("state", "=", "running")]):
        try:
            timeout = get_job(rec.name).timeout_seconds
        except Exception:
            timeout = 600
        if rec.started_at and rec.started_at < now - dt.timedelta(seconds=timeout):
            stuck += 1
            attempt = env["core.job_attempt"].search([("job_id", "=", rec.ids[0])], order="number desc", limit=1)
            attempt.write({"status": "failed", "finished_at": now, "error": "timed out (worker lost or job too slow)"})
            if rec.attempts >= rec.max_attempts:
                rec.write({"state": "dead", "finished_at": now, "error": "timed out"})
            else:
                rec.write({"state": "queued", "next_attempt_at": now, "error": "timed out"})
                requeued += 1
    db_url = env.registry.db_url
    late = jobs.search(
        [("state", "=", "queued"), ("next_attempt_at", "<", now - dt.timedelta(seconds=stale_after_seconds))], limit=500
    )
    known = registered_jobs()
    for rec in late:
        if rec.name in known:
            jid = rec.ids[0]
            env.cr.after_commit(lambda jid=jid: dispatch(db_url, jid))
            redispatched += 1
    old = jobs.search(
        [("state", "in", ["done", "cancelled"]), ("finished_at", "<", now - dt.timedelta(days=keep_days))], limit=5000
    )
    pruned = len(old)
    old.unlink()
    return {"stuck": stuck, "requeued": requeued, "redispatched": redispatched, "pruned": pruned}
