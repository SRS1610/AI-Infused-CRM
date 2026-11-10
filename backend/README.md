# Tessera backend

The `tessera` package is the kernel (metadata ORM, module system, security, API, jobs, realtime, reports, i18n, AI runtime, CLI). Business and platform modules live under `modules/`, one directory each, with a `manifest.toml`.

```bash
uv sync                      # install (creates .venv)
uv run tessera --help        # CLI
uv run pytest                # unit tests (DB tests need TESSERA_TEST_DATABASE_URL)
uv run ruff check . && uv run mypy tessera
```

See `docs/architecture/ARCHITECTURE.md` at the repository root for the design and `docs/dev/` for module authoring.
