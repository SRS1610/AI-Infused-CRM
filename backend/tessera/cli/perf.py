"""Performance checks (U-6): standard list views of the primary models with 100k rows answer in under
300 ms at the 95th percentile.

``tessera perf lists`` fills each primary model up to ``--rows`` records by cloning its existing (demo)
rows in SQL — so no per-record hooks, jobs or messages run — then times the request the web client's
list view sends (the view's fields, default order, first page, total count) as an administrator and as a
salesperson whose record rules apply. Run it on a database prepared with ``tessera seed demo``; the
nightly workflow does exactly that.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import statistics
import time
from pathlib import Path
from typing import Annotated, Any

import typer

app = typer.Typer(no_args_is_help=True)

PRIMARY = ("contacts.partner", "crm.lead", "calendar.event", "sale.quotation", "product.product")

# a first record for models the demo data leaves empty (cloning needs one)
STARTERS: dict[str, Any] = {
    "calendar.event": lambda now: {
        "name": "Weekly pipeline review",
        "start": now,
        "stop": now + dt.timedelta(hours=1),
    },
}


def _unique_text_columns(cr: Any, table: str) -> set[str]:
    """Text columns covered by a unique index (they get a suffix in clones)."""
    rows = cr.fetchall(
        """
        SELECT a.attname, format_type(a.atttypid, a.atttypmod)
          FROM pg_index i
          JOIN pg_class t ON t.oid = i.indrelid
          JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
         WHERE t.relname = :t AND i.indisunique AND NOT i.indisprimary
        """,
        {"t": table},
    )
    return {name for name, typ in rows if typ.startswith(("character varying", "text"))}


def clone_rows(cr: Any, table: str, rows: int) -> int:
    """Bring ``table`` up to ``rows`` rows by copying its existing rows; returns how many were added."""
    current = int(cr.scalar(f'SELECT count(*) FROM "{table}"') or 0)  # noqa: S608
    if current == 0 or current >= rows:
        return 0
    columns = [
        c
        for (c,) in cr.fetchall(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND table_name = :t AND column_name <> 'id' AND is_generated = 'NEVER' ORDER BY ordinal_position",
            {"t": table},
        )
    ]
    unique = _unique_text_columns(cr, table)
    select = ", ".join(f"src.\"{c}\" || '-' || g.n" if c in unique else f'src."{c}"' for c in columns)
    names = ", ".join(f'"{c}"' for c in columns)
    copies = -(-(rows - current) // current)  # ceil
    cr.execute(
        f'INSERT INTO "{table}" ({names}) SELECT {select} FROM "{table}" src '  # noqa: S608
        f"CROSS JOIN generate_series(1, {copies}) AS g(n) LIMIT {rows - current}"
    )
    cr.execute(f'ANALYZE "{table}"')
    return rows - current


def _p(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def measure(client: Any, model: str, runs: int) -> dict[str, Any]:
    views = client.get("/api/v1/web/views", params={"model": model, "types": "list"}).json()
    listing = views["views"]["list"]
    # the client requests every column except the optional ones hidden by default
    names = [c["field"] for c in listing.get("columns", []) if c.get("field") and c.get("optional") != "hide"]
    fields = ",".join(dict.fromkeys(n for n in names if n in views["fields"])) or "display_name"
    params = {"fields": fields, "limit": 80, "offset": 0, "count": "true"}
    for _ in range(3):  # warm caches and the registry
        client.get(f"/api/v1/models/{model}", params=params)
    timings: list[float] = []
    total = None
    for _ in range(runs):
        started = time.perf_counter()
        resp = client.get(f"/api/v1/models/{model}", params=params)
        timings.append((time.perf_counter() - started) * 1000)
        resp.raise_for_status()
        total = resp.json().get("total")
    return {
        "p50_ms": round(statistics.median(timings), 1),
        "p95_ms": round(_p(timings, 0.95), 1),
        "visible_rows": total,
        "fields": fields.count(",") + 1,
    }


@app.command("lists")
def lists(
    rows: Annotated[int, typer.Option(help="Rows per primary model")] = 100_000,
    runs: Annotated[int, typer.Option(help="Timed requests per model and user")] = 40,
    budget_ms: Annotated[float, typer.Option(help="p95 budget (U-6)")] = 300.0,
    users: Annotated[str, typer.Option(help="login:password pairs, comma-separated")] = "admin:admin1234,demo:demo1234",
    database_url: Annotated[str | None, typer.Option()] = None,
    json_out: Annotated[Path | None, typer.Option("--json")] = None,
    seed: Annotated[bool, typer.Option(help="Clone rows up to --rows first")] = True,
) -> None:
    """Fill the primary models up to --rows and time their standard list views; non-zero exit over budget."""
    from fastapi.testclient import TestClient

    from tessera.api.app import create_app
    from tessera.config import get_settings
    from tessera.db.engine import Cursor, get_engine
    from tessera.orm.registry import Registry

    settings = get_settings()
    url = database_url or settings.database_url
    settings.database_url = url
    registry = Registry.get(url)
    present = [m for m in PRIMARY if m in registry.models]
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    if seed:
        from tessera.orm.environment import environment_for

        with environment_for(url, su=True) as env:
            for model, starter in STARTERS.items():
                if model in present and not env[model].search_count([]):
                    env[model].create(starter(env.now().replace(microsecond=0)))
        cr = Cursor(get_engine(url))
        try:
            for model in present:
                added = clone_rows(cr, registry.models[model]._table, rows)
                typer.echo(f"{model}: +{added} rows")
            cr.commit()
        finally:
            cr.close()
    settings.api_rate_limit_per_minute = 1_000_000
    report: list[dict[str, Any]] = []
    failed = False
    for pair in users.split(","):
        login, _, password = pair.partition(":")
        with TestClient(create_app(), base_url="http://localhost") as client:
            resp = client.post("/api/v1/auth/login", json={"login": login, "password": password})
            if resp.status_code != 200:
                typer.echo(f"skipping user {login}: login failed ({resp.status_code})")
                continue
            for model in present:
                result = {"model": model, "user": login, **measure(client, model, runs)}
                result["ok"] = result["p95_ms"] < budget_ms
                failed |= not result["ok"]
                report.append(result)
                typer.echo(
                    f"{'ok  ' if result['ok'] else 'SLOW'} {model:<18} {login:<6} p50 {result['p50_ms']:>6} ms  "
                    f"p95 {result['p95_ms']:>6} ms  ({result['visible_rows']} rows visible, {result['fields']} fields)"
                )
    if json_out:
        json_out.write_text(json.dumps({"rows": rows, "budget_ms": budget_ms, "results": report}, indent=2))
    if failed:
        raise typer.Exit(1)
