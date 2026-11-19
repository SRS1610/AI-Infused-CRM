"""Background job commands: run queued jobs, list, re-queue, run one scheduler tick."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True)
console = Console()


def _url(database_url: str | None) -> str:
    from tessera.config import get_settings

    return database_url or get_settings().database_url


@app.command("run")
def run(
    job_ids: list[int] = typer.Argument(None, help="Job ids (default: every queued job that is due)"),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """Execute queued jobs in this process (useful without a worker, or to drain a queue)."""
    from tessera.jobs import run_now

    results = run_now(_url(database_url), job_ids or None, due_only=not job_ids)
    for jid, state in results.items():
        console.print(f"job {jid}: {state}")
    if not results:
        console.print("no due jobs")


@app.command("list")
def list_jobs(
    state: str | None = typer.Option(None, "--state", help="queued, running, done, dead, cancelled"),
    limit: int = typer.Option(30, "--limit"),
    database_url: str | None = typer.Option(None, "--database-url"),
) -> None:
    """List recent jobs."""
    from tessera.orm.environment import SYSTEM_UID, environment_for

    with environment_for(_url(database_url), SYSTEM_UID, su=True) as env:
        domain = [("state", "=", state)] if state else []
        table = Table("id", "job", "state", "attempts", "next attempt", "error")
        for job in env["core.job"].search(domain, limit=limit):
            table.add_row(
                str(job.ids[0]),
                job.name,
                job.state,
                f"{job.attempts}/{job.max_attempts}",
                str(job.next_attempt_at or ""),
                (job.error or "").splitlines()[0][:60] if job.error else "",
            )
        console.print(table)


@app.command("requeue")
def requeue(job_ids: list[int], database_url: str | None = typer.Option(None, "--database-url")) -> None:
    """Put dead or cancelled jobs back in the queue."""
    from tessera.orm.environment import SYSTEM_UID, environment_for

    with environment_for(_url(database_url), SYSTEM_UID, su=True) as env:
        env["core.job"].browse(job_ids).action_requeue()
    console.print(f"re-queued {len(job_ids)} job(s)")


@app.command("tick")
def tick(database_url: str | None = typer.Option(None, "--database-url")) -> None:
    """Run one scheduler pass (enqueue due scheduled jobs) and exit."""
    from tessera.jobs.scheduler import tick as do_tick

    created = do_tick(_url(database_url))
    console.print(f"enqueued {len(created)} job(s)")
