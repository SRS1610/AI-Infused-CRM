"""``tessera module`` commands."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True)
console = Console()


@app.command("list")
def list_modules(
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """List available modules with their installed state."""
    from tessera.modules.installer import module_states

    table = Table("Module", "Version", "Phase", "State", "Installed version", "Summary")
    for state in module_states(database_url=database_url):
        colour = {"installed": "green", "to upgrade": "yellow"}.get(state.state, "white")
        table.add_row(
            state.name,
            state.version,
            str(state.phase),
            f"[{colour}]{state.state}[/{colour}]",
            state.installed_version or "-",
            state.summary,
        )
    console.print(table)


@app.command()
def install(
    names: list[str] = typer.Argument(..., help="Module names"),
    demo: bool = typer.Option(False, "--demo", help="Load demo data"),
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Install modules (and their dependencies)."""
    from tessera.modules.installer import install_modules

    result = install_modules(names, database_url=database_url, demo=demo)
    console.print(f"[green]Installed[/green] {', '.join(result.installed) or 'nothing new'} in {result.seconds:.1f}s")


@app.command()
def upgrade(
    names: list[str] = typer.Argument(..., help="Module names"),
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Upgrade modules: run migrations, re-sync schema, reload data."""
    from tessera.modules.installer import upgrade_modules

    result = upgrade_modules(names, database_url=database_url)
    console.print(f"[green]Upgraded[/green] {', '.join(result.upgraded)} in {result.seconds:.1f}s")


@app.command()
def uninstall(
    names: list[str] = typer.Argument(..., help="Module names"),
    drop_data: bool = typer.Option(False, "--drop-data", help="Drop columns/tables now instead of renaming them"),
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Uninstall modules. Refuses while other installed modules depend on them."""
    from tessera.modules.installer import uninstall_modules

    result = uninstall_modules(names, database_url=database_url, drop_data=drop_data)
    console.print(f"[yellow]Uninstalled[/yellow] {', '.join(result.uninstalled)}")


@app.command()
def scaffold(
    name: str = typer.Argument(..., help="Technical module name (snake_case)"),
    path: Path = typer.Option(None, "--path", help="Modules directory (defaults to backend/modules)"),
    summary: str = typer.Option("", "--summary"),
    depends: str = typer.Option("core,web,chatter", "--depends", help="Comma-separated dependencies"),
) -> None:
    """Create a new module skeleton: manifest, models, views, security, data, tests, ai."""
    from tessera.modules.scaffold import scaffold_module

    target = scaffold_module(name, path=path, summary=summary, depends=[d.strip() for d in depends.split(",") if d])
    console.print(f"[green]Created[/green] module at {target}")
