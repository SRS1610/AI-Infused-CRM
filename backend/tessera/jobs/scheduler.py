"""Scheduler: turns due ``core.scheduled_job`` rows into jobs. Safe with several replicas: due rows are
claimed with ``FOR UPDATE SKIP LOCKED`` and their ``next_run`` is advanced in the same transaction, and the
generated job carries an idempotency key ``sched:<id>:<run time>`` (D-032)."""

from __future__ import annotations

import datetime as dt
import time

from croniter import croniter

from tessera.config import get_settings
from tessera.db.engine import Cursor, get_engine
from tessera.logging import get_logger

log = get_logger(__name__)


def next_run(cron: str | None, interval_seconds: int | None, after: dt.datetime) -> dt.datetime:
    if cron:
        return croniter(cron, after).get_next(dt.datetime)  # type: ignore[no-any-return]
    return after + dt.timedelta(seconds=max(int(interval_seconds or 60), 1))


def tick(db_url: str, now: dt.datetime | None = None) -> list[int]:
    """Claim every due scheduled job once and enqueue it. Returns the job ids created."""
    from tessera.jobs.api import enqueue
    from tessera.orm.environment import SYSTEM_UID, Environment
    from tessera.orm.registry import Registry

    registry = Registry.get(db_url)
    cr = Cursor(get_engine(db_url))
    created: list[int] = []
    try:
        now = now or dt.datetime.now(dt.UTC)
        rows = cr.fetchall(
            """SELECT id, cron, interval_seconds, next_run FROM core_scheduled_job
               WHERE active AND next_run <= :now ORDER BY next_run FOR UPDATE SKIP LOCKED""",
            {"now": now},
        )
        env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        for sid, cron, interval, due in rows:
            sched = env["core.scheduled_job"].browse([sid])
            job = enqueue(
                env.with_user(sched.user_id.ids[0]) if sched.user_id else env,
                sched.job_name,
                dict(sched.kwargs or {}),
                idempotency_key=f"sched:{sid}:{due.isoformat()}",
                description=sched.name,
            )
            created.extend(job.ids)
            sched.write({"last_run": now, "next_run": next_run(cron, interval, now), "last_job_id": job.ids[0]})
        cr.commit()
        return created
    except Exception:
        cr.rollback()
        raise
    finally:
        cr.close()


def run_scheduler(interval: int = 15, db_url: str | None = None) -> None:  # pragma: no cover - process loop
    url = db_url or get_settings().database_url
    log.info("scheduler started", interval=interval)
    while True:
        try:
            created = tick(url)
            if created:
                log.info("scheduled jobs enqueued", count=len(created))
        except Exception:
            log.exception("scheduler tick failed")
        time.sleep(interval)
