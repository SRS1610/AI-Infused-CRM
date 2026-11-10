"""Process commands: serve, worker, scheduler, shell."""

from __future__ import annotations

import typer
from rich.console import Console

console = Console()


def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port"),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes (development)"),
    workers: int = typer.Option(1, "--workers"),
) -> None:
    """Run the API server."""
    import uvicorn

    from tessera.config import get_settings
    from tessera.logging import configure_logging

    get_settings().check()
    configure_logging()
    uvicorn.run(
        "tessera.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        workers=1 if reload else workers,
        log_config=None,
    )


def worker(
    queues: str = typer.Option("default,mail,webhooks,reports,ai,imports", "--queues", "-Q"),
    concurrency: int = typer.Option(4, "--concurrency", "-c"),
) -> None:
    """Run a Celery worker."""
    from tessera.jobs.celery_app import celery_app
    from tessera.logging import configure_logging

    configure_logging()
    celery_app.worker_main(["worker", "--loglevel=INFO", f"--queues={queues}", f"--concurrency={concurrency}"])


def scheduler(
    interval: int = typer.Option(15, "--interval", help="Seconds between schedule scans"),
) -> None:
    """Run the scheduled-job dispatcher (one active instance, lock-protected)."""
    from tessera.jobs.scheduler import run_scheduler
    from tessera.logging import configure_logging

    configure_logging()
    run_scheduler(interval=interval)


def shell(
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
    uid: int = typer.Option(1, "--uid", help="User id for the environment (1 = system)"),
) -> None:
    """Open an interactive Python shell with ``env`` bound to the database."""
    import code

    from tessera.orm.environment import environment_for

    with environment_for(database_url=database_url, uid=uid) as env:
        banner = f"Tessera shell — env bound to {env.cr.dbname} as uid={uid}. Changes commit on clean exit."
        code.interact(banner=banner, local={"env": env})
