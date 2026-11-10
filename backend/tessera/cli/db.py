"""``tessera db`` commands."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command()
def init(
    modules: str = typer.Option("core", "--modules", "-m", help="Comma-separated modules to install"),
    demo: bool = typer.Option(False, "--demo", help="Load demo data"),
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Create the schema and install the given modules on an empty database."""
    from tessera.modules.installer import install_modules

    names = [m.strip() for m in modules.split(",") if m.strip()]
    result = install_modules(names, database_url=database_url, demo=demo)
    console.print(f"[green]Installed[/green] {', '.join(result.installed)} in {result.seconds:.1f}s")


@app.command()
def migrate(
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Upgrade every installed module whose manifest version is newer than the installed one."""
    from tessera.modules.installer import upgrade_pending

    result = upgrade_pending(database_url=database_url)
    if not result.upgraded:
        console.print("Nothing to upgrade.")
    else:
        console.print(f"[green]Upgraded[/green] {', '.join(result.upgraded)} in {result.seconds:.1f}s")


@app.command()
def check(
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Report schema drift between the registry and the database without changing anything."""
    from tessera.modules.installer import check_schema

    drift = check_schema(database_url=database_url)
    if not drift:
        console.print("[green]No drift.[/green]")
        return
    table = Table("Kind", "Object", "Detail")
    for item in drift:
        table.add_row(item.kind, item.object, item.detail)
    console.print(table)
    raise typer.Exit(code=1)


@app.command()
def reset(
    yes: bool = typer.Option(False, "--yes", help="Do not ask for confirmation"),
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Drop every table in the database (development only)."""
    from tessera.config import get_settings
    from tessera.modules.installer import drop_everything

    settings = get_settings()
    if settings.is_production:
        console.print("[red]Refusing to reset a production database.[/red]")
        raise typer.Exit(code=2)
    if not yes and not typer.confirm("This drops every table. Continue?"):
        raise typer.Exit(code=1)
    drop_everything(database_url=database_url)
    console.print("[yellow]Database reset.[/yellow]")
