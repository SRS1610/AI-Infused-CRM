"""Background jobs (D-008): a database ledger (``core.job``) plus an executor (Celery or in-process).

    from tessera.jobs import job, enqueue

    @job("crm.rescore_leads", queue="ai", max_attempts=3)
    def rescore(env, lead_ids): ...

    enqueue(env, "crm.rescore_leads", {"lead_ids": [1, 2]}, idempotency_key="rescore:2026-11-10")

Jobs are dispatched only after the enqueuing transaction commits; each run executes in its own
transaction as the enqueuing user, so permissions apply to background work too.
"""

from tessera.jobs.api import enqueue, process_job, run_now
from tessera.jobs.registry import JobSpec, get_job, job, registered_jobs

__all__ = ["JobSpec", "enqueue", "get_job", "job", "process_job", "registered_jobs", "run_now"]
