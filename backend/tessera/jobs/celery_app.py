"""Celery application: one generic task executes any ``core.job`` by id."""

from __future__ import annotations

from celery import Celery

from tessera.config import get_settings

settings = get_settings()
celery_app = Celery("tessera", broker=settings.redis_url, backend=None)
celery_app.conf.update(
    task_acks_late=True,  # a worker crash re-delivers the task; the job row lock prevents double runs
    worker_prefetch_multiplier=1,
    task_default_queue="default",
    broker_connection_retry_on_startup=True,
)


@celery_app.task(name="tessera.run_job")  # type: ignore[untyped-decorator]
def run_job(db_url: str, job_id: int) -> str:
    from tessera.jobs.api import process_job

    return process_job(db_url, job_id)
