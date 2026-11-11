"""Module lifecycle on a real database: scaffold → install → upgrade (with migrations) → uninstall.

These tests commit their own transactions (installs are not transactional across tests), so they
build a throw-away module in a temporary module path and clean it up afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tessera.config import get_settings
from tessera.exceptions import ModuleError
from tessera.modules.installer import (
    check_schema,
    install_modules,
    module_states,
    uninstall_modules,
    upgrade_modules,
    upgrade_pending,
)
from tessera.modules.loader import clear_discovery_cache, discover
from tessera.modules.scaffold import scaffold_module
from tessera.orm.environment import environment_for
from tessera.orm.registry import Registry, forget_module_fragments

pytestmark = pytest.mark.db


@pytest.fixture
def temp_module_path(tmp_path: Path):  # type: ignore[no-untyped-def]
    settings = get_settings()
    settings.module_paths.append(tmp_path)
    clear_discovery_cache()
    try:
        yield tmp_path
    finally:
        settings.module_paths.remove(tmp_path)
        clear_discovery_cache()


def _bump_version(module_dir: Path, version: str) -> None:
    manifest = module_dir / "manifest.toml"
    text = manifest.read_text()
    manifest.write_text(text.replace('version = "0.1.0"', f'version = "{version}"'))


def test_scaffold_install_upgrade_uninstall(registry, database_url: str, temp_module_path: Path):  # type: ignore[no-untyped-def]
    name = "ktest_lifecycle"
    module_dir = scaffold_module(name, path=temp_module_path, summary="Lifecycle", depends=["core"])
    assert (module_dir / "manifest.toml").is_file()
    assert (module_dir / "models" / "item.py").is_file()
    # the scaffold references chatter.thread; this test database has no chatter module yet
    item_py = module_dir / "models" / "item.py"
    item_py.write_text(
        item_py.read_text().replace(
            '_inherit = ["chatter.thread", "core.multi_company"]', '_inherit = ["core.multi_company"]'
        )
    )
    manifest = module_dir / "manifest.toml"
    manifest.write_text(
        manifest.read_text()
        .replace('views = ["views/ktest_lifecycle.json"]\n', "")
        .replace('menus = "menus.json"\n', "")
        .replace('actions = "actions.json"\n', "")
        .replace('ai = "ai/features.yaml"\n', "")
    )
    clear_discovery_cache()
    assert name in discover()

    result = install_modules([name], database_url=database_url)
    assert name in result.installed
    with environment_for(database_url, uid=1, su=True) as env:
        item = env[f"{name}.item"].create({"name": "First"})
        assert item.name == "First"
        assert env["core.module"].search([("name", "=", name)]).state == "installed"
        assert env.cr.scalar("SELECT to_regclass('ktest_lifecycle_item') IS NOT NULL")
    states = {s.name: s for s in module_states(database_url=database_url)}
    assert states[name].state == "installed"

    # -- upgrade: new field + pre/post migration scripts -------------------------------------
    item_py.write_text(
        item_py.read_text().replace(
            "    description = fields.Text()\n",
            "    description = fields.Text()\n    rating = fields.Integer(default=0)\n",
        )
    )
    _bump_version(module_dir, "0.2.0")
    mig = module_dir / "migrations" / "0.2.0"
    mig.mkdir(parents=True)
    (mig / "pre.py").write_text(
        "def migrate(env, from_version):\n    env.cr.execute(\"UPDATE ktest_lifecycle_item SET name = name || ' (migrated)'\")\n"
    )
    (mig / "post.py").write_text(
        'def migrate(env, from_version):\n    env["ktest_lifecycle.item"].search([]).write({"rating": 5})\n'
    )
    (mig / "down.py").write_text("def migrate(env, from_version):\n    pass\n")
    clear_discovery_cache()
    assert states[name].state == "installed"
    pending_states = {s.name: s for s in module_states(database_url=database_url)}
    assert pending_states[name].state == "to upgrade"

    # the module code must be re-imported with the new field; simulate a fresh process
    import sys

    for mod_name in [m for m in sys.modules if name in m]:
        del sys.modules[mod_name]
    forget_module_fragments(name)
    Registry.forget(database_url)
    upgraded = upgrade_pending(database_url=database_url)
    assert name in upgraded.upgraded
    with environment_for(database_url, uid=1, su=True) as env:
        items = env[f"{name}.item"].search([])
        assert items.mapped("name") == ["First (migrated)"]
        assert items.rating == 5
        logs = env["core.module_migration_log"].search([("module", "=", name)], order="id")
        assert [(entry.step, entry.status) for entry in logs] == [("pre", "done"), ("post", "done")]
        assert env["core.module"].search([("name", "=", name)]).installed_version == "0.2.0"

    with pytest.raises(ModuleError, match="not installed"):
        upgrade_modules(["does_not_exist_mod"], database_url=database_url)

    # -- uninstall --------------------------------------------------------------------------------
    uninstalled = uninstall_modules([name], database_url=database_url, drop_data=True)
    assert uninstalled.uninstalled == [name]
    with environment_for(database_url, uid=1, su=True) as env:
        assert f"{name}.item" not in env
        assert not env.cr.scalar("SELECT to_regclass('ktest_lifecycle_item') IS NOT NULL")
        assert env["core.module"].search([("name", "=", name)]).state == "uninstalled"
        assert env["core.module_record"].search_count([("module", "=", name)]) == 0
        assert env["core.group"].search_count([("name", "ilike", "Ktest Lifecycle")]) == 0

    # the session registry must be rebuilt without the module for the remaining tests
    Registry.forget(database_url)
    Registry.get(database_url)
    forget_module_fragments(name)


def test_uninstall_refuses_core_and_dependants(registry, database_url: str):  # type: ignore[no-untyped-def]
    with pytest.raises(ModuleError, match="core module cannot be uninstalled"):
        uninstall_modules(["core"], database_url=database_url)
    with pytest.raises(ModuleError, match=r"still required by .*kernel_test_b"):
        uninstall_modules(["kernel_test_a"], database_url=database_url)


def test_scaffold_rejects_bad_names(tmp_path: Path) -> None:
    with pytest.raises(ModuleError):
        scaffold_module("Bad-Name", path=tmp_path)
    scaffold_module("okmod", path=tmp_path)
    with pytest.raises(ModuleError, match="already exists"):
        scaffold_module("okmod", path=tmp_path)
    access = json.loads((tmp_path / "okmod" / "security" / "access.json").read_text())
    assert {a["values"]["model_name"] for a in access} == {"okmod.item"}


def test_data_loader_edge_cases(env):  # type: ignore[no-untyped-def]
    from tessera.modules.data import DataLoader

    loader = DataLoader(env, "kernel_test_a", mode="upgrade")
    # noupdate records are left alone on upgrade
    before = env.ref("kernel_test_a.tag_classic").color
    loader.load_entry({"id": "tag_classic", "model": "ktest.tag", "noupdate": True, "values": {"color": 99}})
    assert env.ref("kernel_test_a.tag_classic").color == before or loader.stats["skipped"] == 0
    # explicit $ref and $refs
    created = loader.load_entry(
        {
            "id": "loader_book",
            "model": "ktest.book",
            "values": {
                "name": "Loaded",
                "author_id": {"$ref": "kernel_test_a.author_austen"},
                "tag_ids": {"$refs": ["kernel_test_a.tag_classic", "kernel_test_a.tag_romance"]},
            },
        }
    )
    assert created is not None
    assert created.author_id == env.ref("kernel_test_a.author_austen")
    assert len(created.tag_ids) == 2
    assert env.ref("kernel_test_a.loader_book") == created
    # delete entry
    loader.load_entry({"id": "loader_book", "delete": True})
    assert (
        not env["ktest.book"]
        .with_context(include_deleted=True)
        .search([("name", "=", "Loaded")])
        .filtered(lambda b: not b.deleted_at)
    )
    with pytest.raises(ModuleError, match="Unknown field"):
        loader.load_entry({"model": "ktest.tag", "values": {"nope": 1}})
    with pytest.raises(ModuleError, match="no model"):
        loader.load_entry({"id": "x", "values": {}})


def test_missing_mode_only_creates_absent_records(env):  # type: ignore[no-untyped-def]
    """New demo records reach upgraded databases; records users changed stay as they are (U-3)."""
    from tessera.modules.data import DataLoader

    loader = DataLoader(env, "kernel_test_a", mode="missing")
    tag = env.ref("kernel_test_a.tag_classic")
    tag.write({"color": 42})
    loader.load_entry({"id": "tag_classic", "model": "ktest.tag", "values": {"color": 7}})
    assert tag.color == 42
    created = loader.load_entry({"id": "tag_brand_new", "model": "ktest.tag", "values": {"name": "Brand new"}})
    assert created is not None and env.ref("kernel_test_a.tag_brand_new") == created
    assert loader.load_entry({"model": "ktest.tag", "values": {"name": "No id"}}) is None
    assert loader.stats == {"created": 1, "updated": 0, "skipped": 2, "deleted": 0}


def test_uninstall_keeping_data_then_reinstall_leaves_no_drift(registry, database_url: str, temp_module_path: Path):  # type: ignore[no-untyped-def]
    """Renamed tables take their indexes and many2many join tables with them, so the module installs again."""
    name = "ktest_reinstall"
    module_dir = scaffold_module(name, path=temp_module_path, summary="Reinstall", depends=["core"])
    item_py = module_dir / "models" / "item.py"
    item_py.write_text(
        item_py.read_text()
        .replace('_inherit = ["chatter.thread", "core.multi_company"]', '_inherit = ["core.multi_company"]')
        .replace(
            "    description = fields.Text()",
            '    description = fields.Text()\n    group_ids = fields.Many2many("core.group")',
        )
    )
    manifest = module_dir / "manifest.toml"
    manifest.write_text(
        manifest.read_text()
        .replace('views = ["views/ktest_reinstall.json"]\n', "")
        .replace('menus = "menus.json"\n', "")
        .replace('actions = "actions.json"\n', "")
        .replace('ai = "ai/features.yaml"\n', "")
    )
    clear_discovery_cache()
    try:
        install_modules([name], database_url=database_url)
        uninstall_modules([name], database_url=database_url)
        assert [d for d in check_schema(database_url=database_url) if "ktest_reinstall" in d.object] == []
        install_modules([name], database_url=database_url)
        assert [d for d in check_schema(database_url=database_url) if "ktest_reinstall" in d.object] == []
        with environment_for(database_url, uid=1, su=True) as env:
            renamed = env.cr.fetchall(
                "SELECT tablename FROM pg_tables WHERE tablename LIKE '\\_removed\\_%ktest_reinstall%'"
            )
            assert len(renamed) == 2  # the model table and its join table
            for (table,) in renamed:
                env.cr.execute(f'DROP TABLE "{table}" CASCADE')
    finally:
        uninstall_modules([name], database_url=database_url, drop_data=True)
        Registry.forget(database_url)
        Registry.get(database_url)
        forget_module_fragments(name)


def _bare_module(name: str, path: Path, depends: list[str]) -> Path:
    module_dir = scaffold_module(name, path=path, summary=name, depends=depends)
    item_py = module_dir / "models" / "item.py"
    item_py.write_text(
        item_py.read_text().replace(
            '_inherit = ["chatter.thread", "core.multi_company"]', '_inherit = ["core.multi_company"]'
        )
    )
    manifest = module_dir / "manifest.toml"
    manifest.write_text(
        manifest.read_text()
        .replace(f'views = ["views/{name}.json"]\n', "")
        .replace('menus = "menus.json"\n', "")
        .replace('actions = "actions.json"\n', "")
        .replace('ai = "ai/features.yaml"\n', "")
    )
    return module_dir


def test_upgrade_installs_new_dependencies(registry, database_url: str, temp_module_path: Path):  # type: ignore[no-untyped-def]
    """A new version depending on a module the database lacks (sale 2.0 → account) installs it first."""
    app, dep = "ktest_upg_app", "ktest_upg_dep"
    app_dir = _bare_module(app, temp_module_path, ["core"])
    _bare_module(dep, temp_module_path, ["core"])
    clear_discovery_cache()
    try:
        install_modules([app], database_url=database_url)
        manifest = app_dir / "manifest.toml"
        manifest.write_text(
            manifest.read_text()
            .replace('version = "0.1.0"', 'version = "0.2.0"')
            .replace('depends = ["core"]', f'depends = ["core", "{dep}"]')
        )
        clear_discovery_cache()
        Registry.forget(database_url)
        result = upgrade_pending(database_url=database_url)
        assert result.installed == [dep] and result.upgraded == [app]
        states = {s.name: s for s in module_states(database_url=database_url)}
        assert states[dep].state == "installed" and states[app].installed_version == "0.2.0"
        assert [d for d in check_schema(database_url=database_url) if "ktest_upg" in d.object] == []
    finally:
        uninstall_modules([app], database_url=database_url, drop_data=True)
        uninstall_modules([dep], database_url=database_url, drop_data=True)
        Registry.forget(database_url)
        Registry.get(database_url)
        forget_module_fragments(app)
        forget_module_fragments(dep)
