# Background jobs and the scheduler

Tessera runs slow or external work — email, webhooks, imports, report rendering, timed automations,
AI calls — as **jobs**. Every job is a row in `core.job` (Settings → Technical → Background jobs) with
its state, arguments, progress, result, last error and one `core.job_attempt` row per try.

## Processes

| Process | Command | Role |
|---|---|---|
| API server | `tessera serve` | Enqueues jobs; with the `inline` backend also runs them in a thread |
| Worker | `tessera worker -Q default,mail,webhooks,reports,ai,imports` | Celery worker executing jobs from Redis |
| Scheduler | `tessera scheduler --interval 15` | Turns due `core.scheduled_job` rows into jobs |

`TESSERA_JOBS_BACKEND` selects how a committed job is dispatched:

* `celery` (production): a task is sent to the job's queue on Redis; workers run it.
* `inline` (development default): a background thread in the API process runs it.
* `manual` (tests): nothing runs until `tessera jobs run` or `run_now()` is called.

Jobs are dispatched **after commit only**: work enqueued by a request that rolls back never runs.

## Several API processes: realtime and presence

With `TESSERA_REALTIME_BACKEND=redis` (several API processes or containers), presence — who is online in
Discuss — is kept in Redis: every process records its WebSocket connections in the sorted set
`tessera:presence:<database>` and refreshes them every 20 seconds, so whichever process answers
`GET /api/v1/discuss/presence` sees every connected user. Connections of a process that stops without
closing them disappear after 60 seconds. If Redis is unreachable, a process answers for its own
connections only. With the in-process backend (`memory`, single process) nothing changes.

## Lifecycle, retries and the dead letter

```
queued → running → done
            └──(error)→ queued (retry after backoff) … → dead (after max attempts)
queued → cancelled
dead / cancelled → (Re-queue) → queued
```

* A job runs as the user who enqueued it, in that user's company — record rules apply. Platform
  maintenance jobs are declared `as_system`.
* Each attempt runs in its own transaction; a failing attempt rolls back everything it wrote.
* Backoff is `backoff_seconds × 2^(attempt−1)`, capped at one hour. Defaults: 5 attempts, 30 s.
* **Idempotency keys** make enqueueing safe to repeat: a second `enqueue(..., idempotency_key=k)`
  returns the existing job. Mail uses `mail:<id>`, webhooks `webhook:<delivery id>`, the scheduler
  `sched:<scheduled job id>:<due time>`.
* Failed (dead) jobs stay visible with their full attempt history; the **Re-queue** button (or
  `tessera jobs requeue <id>`) grants one more attempt.

## The scheduler

`core.scheduled_job` rows hold a 5-field cron expression (UTC) or an interval in seconds. Several
scheduler replicas can run at once: each tick claims due rows with `SELECT … FOR UPDATE SKIP LOCKED`
and advances `next_run` in the same transaction, and the job it creates carries the idempotency key
above — so every occurrence is enqueued exactly once (D-032). **Run now** on a scheduled job enqueues it
immediately without touching its schedule.

Scheduled jobs installed by default:

| Name | Job | Schedule |
|---|---|---|
| Job system sweep | `core.jobs_sweep` | every 5 min |
| Retry failed webhook deliveries | `core.webhook_retry` | every minute |
| Fetch incoming mail (IMAP) | `chatter.fetch_mail` | every 2 min |
| Daily notification digest | `chatter.digest` | 07:00 UTC |
| Run time-based automations | `workflow.run_timed` | every 5 min |

The **sweep** marks `running` jobs whose worker vanished (older than the job's timeout) as failed and
re-queues or dead-letters them, re-dispatches queued jobs whose broker message was lost, and deletes
finished jobs older than 30 days.

## Command line

```bash
tessera jobs list --state dead
tessera jobs run            # run every due queued job in this process
tessera jobs run 42 43      # run specific jobs now
tessera jobs requeue 42
tessera jobs tick           # one scheduler pass
```

## Progress

Long jobs call `job.set_progress(percent, message)`; progress is committed separately and published
on the realtime channel `job:<id>` (subscribers must be able to read the job). The import wizard uses
it to show a live progress bar.

## Writing a job (developers)

```python
from tessera.jobs import enqueue, job

@job("sales.send_reminders", queue="default", max_attempts=3, backoff_seconds=60)
def send_reminders(env, order_ids: list[int]) -> dict:
    ...
    return {"sent": len(order_ids)}

enqueue(env, "sales.send_reminders", {"order_ids": ids}, idempotency_key=f"remind:{day}")
```

Arguments and results must be JSON-serialisable. Raise to fail the attempt; the retry policy applies.
