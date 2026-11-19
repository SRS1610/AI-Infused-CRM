"""``gateway serve`` — run the HTTP API."""

from __future__ import annotations

import typer

cli = typer.Typer(help="Tessera AI gateway", no_args_is_help=True)


@cli.command()
def serve(host: str = "127.0.0.1", port: int = 8100, reload: bool = False) -> None:
    """Serve the gateway API."""
    import uvicorn

    uvicorn.run("gateway.cli:app_factory", factory=True, host=host, port=port, reload=reload)


@cli.command()
def version() -> None:
    """Print the gateway version."""
    from gateway import __version__

    typer.echo(__version__)


def app_factory():  # type: ignore[no-untyped-def]
    from gateway.app import create_app

    return create_app()


def main() -> None:
    cli()
