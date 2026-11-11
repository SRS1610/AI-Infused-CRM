"""``tessera seed demo``: install a phase's modules with demo data and run their seeding steps."""

from __future__ import annotations

import typer
from rich.console import Console

app = typer.Typer(no_args_is_help=True)
console = Console()

PHASE_MODULES = {
    1: [
        "core", "web", "chatter", "workflow", "reports", "contacts", "product", "sale", "crm", "portal",
        "discuss", "calendar", "ai_core", "studio",
    ],
}  # fmt: skip
# each phase's scenario includes the previous ones
PHASE_MODULES[2] = [
    *PHASE_MODULES[1], "account", "account_analytic", "account_budget", "account_asset",
    "account_reports", "account_tax", "account_bank", "payment", "account_dunning", "account_einvoice",
    "auth_saml", "sale_rental", "sale_commission",
]  # fmt: skip


@app.command("demo")
def demo(
    phase: int = typer.Option(1, "--phase", help="Phase whose demo scenario to load"),
    database_url: str | None = typer.Option(None, "--database-url", envvar="TESSERA_DATABASE_URL"),
) -> None:
    """Install the phase's modules with demo data (missing ones only), then let every module seed its
    demo scenario (``seed_demo`` hooks: retrieval index, trained models, sample AI runs…)."""
    from tessera.config import get_settings
    from tessera.modules.installer import install_modules
    from tessera.orm.environment import environment_for

    if phase not in PHASE_MODULES:
        raise typer.BadParameter(f"No demo scenario for phase {phase}")
    url = database_url or get_settings().database_url
    result = install_modules(PHASE_MODULES[phase], database_url=url, demo=True)
    if result.installed:
        console.print(f"[green]Installed[/green] {', '.join(result.installed)} in {result.seconds:.1f}s")
    with environment_for(url, su=True) as env:
        steps = env.registry.hooks.get("seed_demo", [])
        for step in steps:
            name = getattr(step, "__qualname__", repr(step))
            console.print(f"seeding: {getattr(step, '__module__', '')}.{name}")
            step(env)
    console.print(f"[green]Demo scenario for phase {phase} ready[/green] ({len(steps)} seeding steps)")
