"""Job ledger and scheduled jobs (see tessera/jobs)."""

from __future__ import annotations

from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

JOB_STATES = [
    ("queued", "Queued"),
    ("running", "Running"),
    ("done", "Done"),
    ("dead", "Failed (dead letter)"),
    ("cancelled", "Cancelled"),
]


class Job(Model):
    _name = "core.job"
    _description = "Background job"
    _order = "id desc"
    _rec_name = "description"
    _studio = False
    _indexes = [("state_next", ["state", "next_attempt_at"])]
    _sql_constraints = [
        ("idempotency_uniq", "UNIQUE (idempotency_key)", "A job with this idempotency key already exists")
    ]

    name = fields.Char("Job", required=True, index=True)
    description = fields.Char()
    kwargs = fields.Json(default=dict)
    queue = fields.Char(default="default")
    state = fields.Selection(JOB_STATES, default="queued", required=True, index=True)
    attempts = fields.Integer(default=0)
    max_attempts = fields.Integer(default=5)
    next_attempt_at = fields.Datetime()
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    idempotency_key = fields.Char(index=True)
    user_id = fields.Many2one("core.user", ondelete="set_null", fk=False)
    company_id = fields.Many2one("core.company", ondelete="set_null", fk=False)
    progress = fields.Integer(default=0, help="0-100")
    progress_message = fields.Char()
    result = fields.Json()
    error = fields.Text()
    attempt_ids = fields.One2many("core.job_attempt", "job_id", "Attempts")

    @api.expose
    def action_requeue(self) -> bool:
        """Put a dead or cancelled job back in the queue (keeps its attempt history)."""
        from tessera.jobs.api import dispatch

        for job in self:
            if job.state not in ("dead", "cancelled"):
                raise UserError(_("Only failed or cancelled jobs can be re-queued"))
            job.write(
                {
                    "state": "queued",
                    "max_attempts": job.attempts + max(job.max_attempts - job.attempts, 1),
                    "next_attempt_at": self.env.now(),
                }
            )
            db_url, jid = self.env.registry.db_url, job.ids[0]
            self.env.cr.after_commit(lambda db_url=db_url, jid=jid: dispatch(db_url, jid))
        return True

    @api.expose
    def action_cancel(self) -> bool:
        for job in self:
            if job.state != "queued":
                raise UserError(_("Only queued jobs can be cancelled"))
        self.write({"state": "cancelled"})
        return True

    def set_progress(self, percent: int, message: str | None = None) -> None:
        """Record progress from inside a job (committed separately so watchers see it) and publish it."""
        from tessera.db.engine import Cursor
        from tessera.realtime import publish

        self.ensure_one()
        cr = Cursor(self.env.cr.engine)
        try:
            cr.execute(
                "UPDATE core_job SET progress = :p, progress_message = :m WHERE id = :id",
                {"p": int(percent), "m": message, "id": self.ids[0]},
            )
            cr.commit()
        finally:
            cr.close()
        publish(self.env.cr.dbname, f"job:{self.ids[0]}", "progress", {"progress": int(percent), "message": message})


class JobAttempt(Model):
    _name = "core.job_attempt"
    _description = "Job attempt"
    _order = "job_id, number"
    _rec_name = "status"
    _studio = False
    _log_access = False

    job_id = fields.Many2one("core.job", required=True, ondelete="cascade", index=True)
    number = fields.Integer(required=True)
    status = fields.Selection([("running", "Running"), ("done", "Done"), ("failed", "Failed")], required=True)
    started_at = fields.Datetime()
    finished_at = fields.Datetime()
    error = fields.Text()


class ScheduledJob(Model):
    _name = "core.scheduled_job"
    _description = "Scheduled job"
    _order = "name"
    _audit = True

    name = fields.Char(required=True)
    job_name = fields.Char("Job", required=True)
    kwargs = fields.Json(default=dict)
    cron = fields.Char(help="5-field cron expression in UTC, e.g. '*/5 * * * *'")
    interval_seconds = fields.Integer(help="Used when no cron expression is set")
    next_run = fields.Datetime(required=True, default=lambda self: self.env.now(), index=True)
    last_run = fields.Datetime(readonly=True)
    last_job_id = fields.Many2one("core.job", readonly=True, ondelete="set_null", fk=False)
    user_id = fields.Many2one("core.user", "Run as", ondelete="set_null", help="Empty = system")
    active = fields.Boolean(default=True)

    @api.constrains("cron", "interval_seconds", "job_name")
    def _check_schedule(self) -> None:
        from croniter import croniter

        from tessera.jobs.registry import registered_jobs

        for sched in self:
            if not sched.cron and not sched.interval_seconds:
                raise ValidationError(_("Give a cron expression or an interval"), field="cron")
            if sched.cron and not croniter.is_valid(sched.cron):
                raise ValidationError(_("Invalid cron expression '{cron}'", cron=sched.cron), field="cron")
            if registered_jobs() and sched.job_name not in registered_jobs():
                raise ValidationError(_("Unknown job '{job_name}'", job_name=sched.job_name), field="job_name")

    @api.expose
    def action_run_now(self) -> dict[str, Any]:
        from tessera.jobs.api import enqueue

        self.ensure_one()
        job = enqueue(self.env, self.job_name, dict(self.kwargs or {}), description=f"{self.name} (manual)")
        return {"job_id": job.ids[0]}
