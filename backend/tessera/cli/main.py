"""``tessera`` CLI entry point.

Sub-commands are registered by their own modules so that heavy imports (uvicorn, celery)
only happen when the corresponding command runs.
"""

from __future__ import annotations

import typer
from rich.console import Console

from tessera import __version__

app = typer.Typer(
    name="tessera",
    help="Tessera developer and operations CLI.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()


def _version(value: bool) -> None:
    if value:
        console.print(f"tessera {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    _version_flag: bool = typer.Option(False, "--version", "-V", callback=_version, is_eager=True, help="Show version"),
) -> None:
    """Tessera developer and operations CLI."""


from tessera.cli import ai as _ai  # noqa: E402
from tessera.cli import db as _db  # noqa: E402
from tessera.cli import i18n as _i18n  # noqa: E402
from tessera.cli import jobs as _jobs  # noqa: E402
from tessera.cli import module as _module  # noqa: E402
from tessera.cli import perf as _perf  # noqa: E402
from tessera.cli import run as _run  # noqa: E402
from tessera.cli import seed as _seed  # noqa: E402

app.add_typer(_db.app, name="db", help="Database: init, migrate, check, reset")
app.add_typer(_i18n.app, name="i18n", help="Translations: extract catalogues, completeness, load into a database")
app.add_typer(_module.app, name="module", help="Modules: scaffold, install, upgrade, uninstall, list")
app.add_typer(_jobs.app, name="jobs", help="Background jobs: run, list, requeue, tick")
app.add_typer(_seed.app, name="seed", help="Demo data: install a phase's scenario")
app.add_typer(_ai.app, name="ai", help="AI: evaluations, completeness report, classification, re-index")
app.add_typer(_perf.app, name="perf", help="Performance checks: list views at volume (U-6)")
app.command("serve")(_run.serve)
app.command("worker")(_run.worker)
app.command("scheduler")(_run.scheduler)
app.command("shell")(_run.shell)


if __name__ == "__main__":  # pragma: no cover
    app()
