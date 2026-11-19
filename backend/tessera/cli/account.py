"""``tessera account``: install charts of accounts and verify the ledger's hash chains (D-073)."""

from __future__ import annotations

import json
from typing import Any

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


@app.command("import-statement")
def import_statement_cmd(
    file: str = typer.Argument(..., help="Statement file (CSV, OFX, CAMT.053 or MT940)"),
    journal: str = typer.Option(..., "--journal", help="Bank journal code (e.g. BNK1)"),
    company: str | None = typer.Option(None, "--company", help="Company name or id (default: the only match)"),
    file_format: str | None = typer.Option(None, "--format", help="camt053, ofx, mt940 or csv (default: detected)"),
    no_post: bool = typer.Option(False, "--no-post", help="Leave the statement open (no posting, no rules)"),
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Import a bank statement file into a journal (requires the ``account_bank`` module)."""
    from pathlib import Path

    try:
        from modules.account_bank.importing import import_statements
    except ImportError as exc:  # pragma: no cover - module not on the path
        raise typer.BadParameter("The account_bank module is not available") from exc

    path = Path(file)
    if not path.is_file():
        raise typer.BadParameter(f"No file {file!r}")
    with _env(database_url) as env:
        if "account.bank_statement" not in env.registry.models:
            raise typer.BadParameter("Install the account_bank module first")
        domain: list[Any] = [("code", "=", journal), ("type", "in", ["bank", "cash"])]
        if company:
            companies = env["core.company"]
            found = (
                companies.browse([int(company)]) if company.isdigit() else companies.search([("name", "=", company)])
            )
            domain.append(("company_id", "in", found.ids))
        journals = env["account.journal"].search(domain)
        if len(journals) != 1:
            raise typer.BadParameter(f"{len(journals)} bank journals match {journal!r}; use --company")
        attachment = env["core.attachment"].create(
            {"name": path.name, "raw": path.read_bytes(), "company_id": journals.company_id.ids[0]}
        )
        result = import_statements(
            journals.with_company(journals.company_id).env["account.journal"].browse(journals.ids),
            path.read_bytes(),
            path.name,
            file_format=file_format,
            attachment=attachment,
            post=not no_post,
            auto_reconcile=not no_post,
        )
        console.print(
            f"[green]{result['format']}[/green]: {result['lines']} transactions imported, "
            f"{result['duplicates']} already imported, {result['reconciled']} reconciled automatically"
        )


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
