"""Install, upgrade and uninstall modules; load the registry of an existing database."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.version import Version

from tessera.config import get_settings
from tessera.db.engine import Cursor, get_engine
from tessera.exceptions import ModuleError
from tessera.logging import get_logger
from tessera.modules.data import DataLoader
from tessera.modules.graph import auto_install_candidates, dependants_of, resolve_order
from tessera.modules.loader import discover, load_module_code
from tessera.modules.manifest import Manifest
from tessera.orm.environment import SYSTEM_UID, Environment
from tessera.orm.fields import Many2many
from tessera.orm.registry import Registry
from tessera.orm.schema import Drift, check_drift, sync_schema

log = get_logger(__name__)

RECORD_KINDS = ("security", "data")
EXTRA_KIND_ORDER = ["views", "reports", "actions", "menus", "ai", "i18n"]


@dataclass
class InstallResult:
    installed: list[str] = field(default_factory=list)
    upgraded: list[str] = field(default_factory=list)
    uninstalled: list[str] = field(default_factory=list)
    seconds: float = 0.0


@dataclass
class ModuleState:
    name: str
    version: str
    phase: int
    state: str
    installed_version: str | None
    summary: str


# -- helpers -----------------------------------------------------------------------------------


def _table_exists(cr: Cursor, table: str) -> bool:
    return bool(cr.scalar("SELECT to_regclass(:t) IS NOT NULL", {"t": table}))


def _installed_modules(cr: Cursor) -> dict[str, str]:
    if not _table_exists(cr, "core_module"):
        return {}
    rows = cr.fetchall("SELECT name, installed_version FROM core_module WHERE state = 'installed'")
    return {r[0]: r[1] or "0" for r in rows}


def _resolve_url(database_url: str | None) -> str:
    return database_url or get_settings().database_url


def load_installed(registry: Registry) -> None:
    """Populate ``registry`` from the modules recorded as installed in its database."""
    engine = get_engine(registry.db_url)
    cr = Cursor(engine)
    try:
        installed = _installed_modules(cr)
    finally:
        cr.close()
    if not installed:
        registry.ready = False
        return
    manifests = discover()
    missing = [m for m in installed if m not in manifests]
    if missing:
        raise ModuleError(f"Installed modules without source code on the module path: {', '.join(sorted(missing))}")
    for name in resolve_order(manifests, list(installed)):
        load_module_code(registry, manifests[name])
    registry.installed_versions = dict(installed)
    registry.build()


def _run_module_hook(manifest: Manifest, env: Environment, hook_ref: str | None, **kwargs: Any) -> None:
    """``post_init = "hooks:post_init"`` → ``modules.<name>.hooks.post_init(env)``."""
    if not hook_ref:
        return
    module_path, _, func_name = hook_ref.partition(":")
    from tessera.modules.loader import import_prefix_for

    prefix = import_prefix_for(manifest.path.parent)
    mod = importlib.import_module(f"{prefix}.{manifest.name}.{module_path}")
    getattr(mod, func_name)(env, **kwargs)


def _load_records(env: Environment, registry: Registry, manifest: Manifest, *, mode: str, demo: bool) -> None:
    kinds = list(RECORD_KINDS) + (["demo"] if demo else [])
    for kind in kinds:
        loader = DataLoader(env, manifest.name, mode=mode)
        for path in manifest.files(kind):
            loader.load_file(path)
    # fixed order so references resolve: views before actions (view refs), actions before menus
    ordered = sorted(
        manifest.load.items(), key=lambda kv: EXTRA_KIND_ORDER.index(kv[0]) if kv[0] in EXTRA_KIND_ORDER else 99
    )
    for kind, files in ordered:
        if kind in RECORD_KINDS or kind == "demo":
            continue
        handler = registry.data_loaders.get(kind)
        if handler is None:
            log.warning("no loader for manifest kind; skipped", module=manifest.name, kind=kind)
            continue
        for rel in files:
            handler(env, manifest, manifest.path / rel, mode)
    env.flush()


def _upsert_module_rows(env: Environment, manifests: dict[str, Manifest]) -> None:
    """Make sure every available module has a ``core.module`` row."""
    modules = env["core.module"]
    existing = {m.name: m for m in modules.with_context(active_test=False).search([])}
    for manifest in manifests.values():
        vals = {
            "name": manifest.name,
            "version": manifest.version,
            "summary": manifest.summary,
            "description": manifest.description,
            "category": manifest.category,
            "phase": manifest.phase,
            "depends": list(manifest.depends),
            "auto_install": manifest.auto_install,
            "application": manifest.application,
            "installable": manifest.installable,
        }
        if manifest.name in existing:
            existing[manifest.name].write(vals)
        else:
            modules.create({**vals, "state": "uninstalled"})


def _sync_model_metadata(env: Environment, registry: Registry) -> None:
    registry.run_hooks("sync_metadata", env)


# -- install -----------------------------------------------------------------------------------


def install_modules(names: Sequence[str], *, database_url: str | None = None, demo: bool = False) -> InstallResult:
    started = time.perf_counter()
    url = _resolve_url(database_url)
    manifests = discover()
    engine = get_engine(url)
    result = InstallResult()
    cr = Cursor(engine)
    try:
        ensure_extensions(cr)
        installed = _installed_modules(cr)
        wanted = list(dict.fromkeys([*names, *installed]))
        order = resolve_order(manifests, wanted)
        # auto-install modules whose dependencies are now satisfied
        changed = True
        while changed:
            changed = False
            for candidate in auto_install_candidates(manifests, order):
                order = resolve_order(manifests, [*order, candidate])
                changed = True
        to_install = [m for m in order if m not in installed]
        registry = Registry.new(url)
        for name in order:
            load_module_code(registry, manifests[name])
        registry.installed_versions = dict(installed)
        registry.build()
        sync_schema(cr, registry)
        env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        _upsert_module_rows(env, manifests)
        for name in to_install:
            manifest = manifests[name]
            log.info("installing module", module=name, version=manifest.version)
            module_rec = env["core.module"].search([("name", "=", name)])
            module_rec.write({"state": "installing"})
            _load_records(env, registry, manifest, mode="install", demo=demo)
            _run_module_hook(manifest, env, manifest.post_init)
            module_rec.write({"state": "installed", "installed_version": manifest.version, "installed_at": env.now()})
            registry.installed_versions[name] = manifest.version
            registry.run_hooks("module_installed", env, name)
            result.installed.append(name)
        _sync_model_metadata(env, registry)
        env.flush()
        cr.commit()
    except BaseException:
        cr.rollback()
        Registry.forget(url)
        raise
    finally:
        cr.close()
    Registry.get(url)  # ensure a fresh registry reflecting the installed state is cached
    result.seconds = time.perf_counter() - started
    return result


# -- upgrade -----------------------------------------------------------------------------------


def _migration_scripts(manifest: Manifest, from_version: str, to_version: str) -> list[tuple[Version, Path]]:
    folder = manifest.path / "migrations"
    if not folder.is_dir():
        return []
    out: list[tuple[Version, Path]] = []
    lo, hi = Version(from_version), Version(to_version)
    for child in folder.iterdir():
        if not child.is_dir():
            continue
        try:
            ver = Version(child.name)
        except Exception:  # noqa: S112 - directories that are not versions are simply skipped
            continue
        if lo < ver <= hi:
            out.append((ver, child))
    return sorted(out, key=lambda x: x[0])


def _run_script(path: Path, env: Environment, module: str, from_version: str) -> None:
    if not path.is_file():
        return
    spec = importlib.util.spec_from_file_location(f"migration_{module}_{path.parent.name}_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ModuleError(f"Cannot load migration {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    migrate = getattr(mod, "migrate", None)
    if migrate is None:
        raise ModuleError(f"Migration {path} has no migrate(env, from_version) function")
    migrate(env, from_version)


def upgrade_modules(names: Sequence[str], *, database_url: str | None = None) -> InstallResult:
    started = time.perf_counter()
    url = _resolve_url(database_url)
    manifests = discover(refresh=True)
    engine = get_engine(url)
    result = InstallResult()
    cr = Cursor(engine)
    try:
        installed = _installed_modules(cr)
        for name in names:
            if name not in installed:
                raise ModuleError(f"Module '{name}' is not installed")
        order = resolve_order(manifests, list(installed))
        registry = Registry.new(url)
        for name in order:
            load_module_code(registry, manifests[name])
        registry.installed_versions = dict(installed)
        registry.build()
        env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        targets = [m for m in order if m in names]
        for name in targets:
            manifest = manifests[name]
            from_version = installed[name]
            log.info("upgrading module", module=name, from_version=from_version, to_version=manifest.version)
            logs = env["core.module_migration_log"]
            for ver, folder in _migration_scripts(manifest, from_version, manifest.version):
                entry = logs.create(
                    {
                        "module": name,
                        "from_version": from_version,
                        "to_version": str(ver),
                        "step": "pre",
                        "status": "running",
                    }
                )
                _run_script(folder / "pre.py", env, name, from_version)
                entry.write({"status": "done", "finished_at": env.now()})
        sync_schema(cr, registry)
        _upsert_module_rows(env, manifests)
        for name in targets:
            manifest = manifests[name]
            from_version = installed[name]
            _load_records(env, registry, manifest, mode="upgrade", demo=False)
            logs = env["core.module_migration_log"]
            for ver, folder in _migration_scripts(manifest, from_version, manifest.version):
                entry = logs.create(
                    {
                        "module": name,
                        "from_version": from_version,
                        "to_version": str(ver),
                        "step": "post",
                        "status": "running",
                    }
                )
                _run_script(folder / "post.py", env, name, from_version)
                entry.write({"status": "done", "finished_at": env.now()})
            env["core.module"].search([("name", "=", name)]).write(
                {"installed_version": manifest.version, "state": "installed"}
            )
            registry.installed_versions[name] = manifest.version
            registry.run_hooks("module_upgraded", env, name)
            result.upgraded.append(name)
        _sync_model_metadata(env, registry)
        env.flush()
        cr.commit()
    except BaseException:
        cr.rollback()
        Registry.forget(url)
        raise
    finally:
        cr.close()
    Registry.get(url)
    result.seconds = time.perf_counter() - started
    return result


def upgrade_pending(*, database_url: str | None = None) -> InstallResult:
    url = _resolve_url(database_url)
    manifests = discover(refresh=True)
    cr = Cursor(get_engine(url))
    try:
        installed = _installed_modules(cr)
    finally:
        cr.close()
    pending = [n for n, v in installed.items() if n in manifests and Version(manifests[n].version) > Version(v)]
    if not pending:
        return InstallResult()
    return upgrade_modules(pending, database_url=url)


# -- uninstall ---------------------------------------------------------------------------------


def uninstall_modules(
    names: Sequence[str], *, database_url: str | None = None, drop_data: bool = False
) -> InstallResult:
    started = time.perf_counter()
    url = _resolve_url(database_url)
    manifests = discover()
    engine = get_engine(url)
    result = InstallResult()
    cr = Cursor(engine)
    try:
        installed = _installed_modules(cr)
        for name in names:
            if name == "core":
                raise ModuleError("The core module cannot be uninstalled")
            if name not in installed:
                raise ModuleError(f"Module '{name}' is not installed")
            blockers = [d for d in dependants_of(manifests, name, installed) if d not in names]
            if blockers:
                raise ModuleError(f"Cannot uninstall '{name}': still required by {', '.join(blockers)}")
        registry = Registry.get(url)
        env = Environment.new(registry, cr, SYSTEM_UID, su=True)
        order = [m for m in reversed(resolve_order(manifests, list(installed))) if m in names]
        owned_tables: list[str] = []
        owned_m2m: set[str] = set()
        owned_columns: list[tuple[str, str]] = []
        for name in order:
            manifest = manifests[name]
            _run_module_hook(manifest, env, manifest.pre_uninstall)
            registry.run_hooks("module_uninstalling", env, name)
            rows = cr.fetchall(
                "SELECT model, res_id FROM core_module_record WHERE module = :m ORDER BY id DESC", {"m": name}
            )
            for model_name, res_id in rows:
                if model_name in env:
                    rec = (
                        env[model_name]
                        .with_context(active_test=False, include_deleted=True, force_delete=True)
                        .browse([res_id])
                        .exists()
                    )
                    if rec:
                        rec.unlink()
            cr.execute("DELETE FROM core_module_record WHERE module = :m", {"m": name})
            for cls in registry.models.values():
                owned_model = cls._module == name and cls._auto
                if owned_model:
                    owned_tables.append(cls._table)
                for fld in cls._fields.values():
                    relation = getattr(fld, "relation", None)
                    if isinstance(fld, Many2many) and relation and (owned_model or fld.module == name):
                        owned_m2m.add(relation)
                    elif not owned_model and fld.module == name and fld.store and fld.sql_type and fld.name != "id":
                        owned_columns.append((cls._table, fld.column_name))
            env["core.module"].search([("name", "=", name)]).write({"state": "uninstalled", "installed_version": None})
            result.uninstalled.append(name)
        env.flush()
        stamp = time.strftime("%Y%m%d%H%M%S")
        for table, column in owned_columns:
            if drop_data:
                cr.execute(f'ALTER TABLE "{table}" DROP COLUMN IF EXISTS "{column}"')
            else:
                cr.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{column}" TO "_removed_{stamp}_{column}"'[:200])
        # join tables still used by a field of a remaining module stay
        remaining = {
            getattr(f, "relation", None)
            for cls in registry.models.values()
            if not (cls._module in result.uninstalled and cls._auto)
            for f in cls._fields.values()
            if isinstance(f, Many2many) and f.module not in result.uninstalled
        }
        tables = owned_tables + sorted(owned_m2m - remaining - set(owned_tables))
        existing = {r[0] for r in cr.fetchall("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")}
        for table in tables:
            if table not in existing:
                continue
            if drop_data:
                cr.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
                continue
            # indexes (and the constraints they back) keep their names across a rename: move them aside too,
            # so installing the module again can create its own
            for (index,) in cr.fetchall(
                "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() AND tablename = :t", {"t": table}
            ):
                digest = hashlib.sha1(index.encode(), usedforsecurity=False).hexdigest()[:10]
                cr.execute(f'ALTER INDEX "{index}" RENAME TO "_removed_{stamp}_{digest}"')
            cr.execute(f'ALTER TABLE "{table}" RENAME TO "_removed_{stamp}_{table}"'[:200])
        cr.commit()
    except BaseException:
        cr.rollback()
        raise
    finally:
        cr.close()
    Registry.forget(url)
    Registry.get(url)
    result.seconds = time.perf_counter() - started
    return result


# -- inspection -------------------------------------------------------------------------------


def check_schema(*, database_url: str | None = None) -> list[Drift]:
    url = _resolve_url(database_url)
    registry = Registry.get(url)
    cr = Cursor(get_engine(url))
    try:
        return check_drift(cr, registry)
    finally:
        cr.close()


def module_states(*, database_url: str | None = None) -> list[ModuleState]:
    url = _resolve_url(database_url)
    manifests = discover(refresh=True)
    cr = Cursor(get_engine(url))
    try:
        installed = _installed_modules(cr)
    finally:
        cr.close()
    out: list[ModuleState] = []
    for name, manifest in sorted(manifests.items()):
        if name in installed:
            state = "to upgrade" if Version(manifest.version) > Version(installed[name]) else "installed"
        else:
            state = "available" if manifest.installable else "not installable"
        out.append(ModuleState(name, manifest.version, manifest.phase, state, installed.get(name), manifest.summary))
    return out


EXTENSIONS = ("vector", "pg_trgm", "unaccent", "btree_gin", "pgcrypto")


def ensure_extensions(cr: Cursor) -> list[str]:
    """Create the PostgreSQL extensions modules rely on (trigram search, vectors, …). Returns those that could
    not be created (missing from the server or insufficient privileges) so callers can warn."""
    missing = []
    for ext in EXTENSIONS:
        if cr.scalar("SELECT 1 FROM pg_extension WHERE extname = :e", {"e": ext}):
            continue
        try:
            with cr.savepoint():
                cr.execute(f"CREATE EXTENSION IF NOT EXISTS {ext}")
        except Exception:  # reported below; pg_trgm is the one most indexes need
            missing.append(ext)
    if missing:
        log.warning("database extensions unavailable", extensions=missing)
    return missing


def drop_everything(*, database_url: str | None = None) -> None:
    url = _resolve_url(database_url)
    cr = Cursor(get_engine(url))
    try:
        cr.execute("DROP SCHEMA public CASCADE")
        cr.execute("CREATE SCHEMA public")
        ensure_extensions(cr)
        cr.commit()
    finally:
        cr.close()
    Registry.forget(url)
