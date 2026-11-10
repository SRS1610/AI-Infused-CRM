"""``tessera i18n`` commands: extract catalogues, report completeness, load translations into a database."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(no_args_is_help=True)
console = Console()


def _modules(names: list[str] | None, every: bool) -> list[str]:
    from tessera.modules.loader import discover

    manifests = discover()
    if every or not names:
        return sorted(manifests)
    unknown = [n for n in names if n not in manifests]
    if unknown:
        raise typer.BadParameter(f"unknown module(s): {', '.join(unknown)}")
    return list(names)


@app.command()
def extract(
    names: list[str] = typer.Argument(None, help="Modules to extract (default: every module on the module path)"),
    lang: list[str] = typer.Option([], "--lang", "-l", help="Also create/update i18n/<lang>.po (repeatable)"),
) -> None:
    """Write i18n/<module>.pot from the module's sources and update its existing <lang>.po files."""
    from tessera.i18n.extract import extract_module, write_module_catalogs
    from tessera.modules.loader import discover

    manifests = discover()
    for name in _modules(names, False):
        extraction = extract_module(name, manifests)
        changed = write_module_catalogs(extraction, manifests[name].path, lang)
        files = ", ".join(p.name for p in changed) or "up to date"
        console.print(f"[green]{name}[/green]: {len(extraction.entries)} strings ({files})")


@app.command()
def stats(
    names: list[str] = typer.Argument(None, help="Modules (default: all)"),
    lang: str = typer.Option("fr_FR", "--lang", "-l"),
    show_missing: bool = typer.Option(False, "--missing", help="List the untranslated strings"),
) -> None:
    """Completeness of the <lang> catalogues against a fresh extraction."""
    from tessera.i18n.extract import completeness, extract_module
    from tessera.modules.loader import discover

    manifests = discover()
    table = Table("Module", "Strings", "Translated", "%", "Placeholder errors")
    total = done = 0
    reports = []
    for name in _modules(names, False):
        report = completeness(extract_module(name, manifests), manifests[name].path, lang)
        reports.append(report)
        total, done = total + report.total, done + report.translated
        colour = "green" if report.ratio >= 0.95 else "red"
        table.add_row(
            name,
            str(report.total),
            str(report.translated),
            f"[{colour}]{report.ratio * 100:.1f}[/{colour}]",
            str(len(report.placeholder_errors)),
        )
    table.add_row("total", str(total), str(done), f"{(done / total * 100) if total else 100:.1f}", "")
    console.print(table)
    if show_missing:
        for report in reports:
            for msgid in report.missing:
                console.print(f"{report.module}: {msgid!r}", markup=False)


@app.command()
def load(
    names: list[str] = typer.Argument(None, help="Installed modules (default: all installed)"),
    lang: list[str] = typer.Option([], "--lang", "-l", help="Only these languages (default: every catalogue)"),
    overwrite: bool = typer.Option(
        False, "--overwrite", help="Also replace existing translations of records flagged noupdate"
    ),
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """(Re)apply the modules' catalogues to the translatable records (menus, actions, stages, ...) of a database."""
    from tessera.i18n.apply import apply_translations
    from tessera.orm.environment import SYSTEM_UID, environment_for

    with environment_for(database_url, uid=SYSTEM_UID, su=True) as env:
        results = apply_translations(env, names or None, langs=lang or None, overwrite=overwrite)
    records = sum(r.records for r in results.values())
    values = sum(r.values for r in results.values())
    console.print(f"[green]Translations loaded[/green]: {values} values on {records} records ({len(results)} modules)")
