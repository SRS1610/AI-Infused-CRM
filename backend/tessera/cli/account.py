"""``tessera account``: install charts of accounts and verify the ledger's hash chains (D-073)."""

from __future__ import annotations

import json

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True)
console = Console()


def _env(database_url: str | None):  # type: ignore[no-untyped-def]
    from tessera.config import get_settings
    from tessera.orm.environment import environment_for

    return environment_for(database_url or get_settings().database_url, su=True)


@app.command("charts")
def charts() -> None:
    """List the available chart-of-accounts templates."""
    from modules.account.chart import available_charts

    for code, data in available_charts().items():
        console.print(f"{code:10} {data['name']}")


@app.command("install-chart")
def install_chart_cmd(
    company: str = typer.Argument(..., help="Company name or id"),
    chart: str | None = typer.Option(None, "--chart", help="Template code (default: from the company's country)"),
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Install a chart of accounts on a company (idempotent)."""
    from modules.account.chart import install_chart

    with _env(database_url) as env:
        companies = env["core.company"]
        found = companies.browse([int(company)]) if company.isdigit() else companies.search([("name", "=", company)])
        if not found:
            raise typer.BadParameter(f"No company {company!r}")
        counts = install_chart(found, chart)
        console.print(f"[green]{found.name}[/green]: chart {found.chart_template} — {json.dumps(counts)}")


@app.command("integrity")
def integrity(
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Recompute the hash chain of every hash-locked journal; exit 1 when one is broken."""
    from modules.account.integrity import check_company

    broken = False
    with _env(database_url) as env:
        for company in env["core.company"].search([("chart_template", "!=", None)]):
            for row in check_company(env, company):
                ok = row["ok"]
                broken |= not ok
                status = "[green]ok[/green]" if ok else f"[red]broken at {row['first_broken']}[/red]"
                console.print(f"{company.name} / {row['journal']}: {row['checked']} entries {status}")
    raise typer.Exit(1 if broken else 0)
