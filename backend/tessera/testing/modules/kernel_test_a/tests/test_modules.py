"""Module system: manifests, dependency graph, registry composition, install state."""

from __future__ import annotations

from pathlib import Path

import pytest

from tessera.exceptions import ModuleError, RegistryError
from tessera.modules.graph import auto_install_candidates, closure, dependants_of, resolve_order
from tessera.modules.manifest import Manifest, load_manifest


def _m(name: str, depends: list[str] | None = None, **kw: object) -> Manifest:
    return Manifest(
        name=name,
        path=Path("/tmp") / name,
        depends=["core", *(depends or [])] if name != "core" else [],
        **kw,
    )  # type: ignore[arg-type]


def test_resolve_order_topological_and_stable() -> None:
    manifests = {
        m.name: m
        for m in [
            _m("core"),
            _m("chatter"),
            _m("web"),
            _m("contacts", ["chatter", "web"]),
            _m("crm", ["contacts"]),
            _m("sale", ["crm"]),
        ]
    }
    assert resolve_order(manifests, ["sale"]) == ["core", "chatter", "web", "contacts", "crm", "sale"]
    assert closure(manifests, ["contacts"]) == {"core", "chatter", "web", "contacts"}
    assert dependants_of(manifests, "contacts", ["core", "chatter", "web", "contacts", "crm", "sale"]) == [
        "crm",
        "sale",
    ]


def test_resolve_order_detects_cycle_and_unknown() -> None:
    a = _m("a", ["b"])
    b = _m("b", ["a"])
    with pytest.raises(ModuleError, match="cycle"):
        resolve_order({"core": _m("core"), "a": a, "b": b}, ["a"])
    with pytest.raises(ModuleError, match="Unknown module"):
        resolve_order({"core": _m("core")}, ["ghost"])


def test_auto_install_candidates() -> None:
    manifests = {
        m.name: m for m in [_m("core"), _m("crm"), _m("sign"), _m("sign_crm", ["crm", "sign"], auto_install=True)]
    }
    assert auto_install_candidates(manifests, ["core", "crm"]) == []
    assert auto_install_candidates(manifests, ["core", "crm", "sign"]) == ["sign_crm"]


def test_load_manifest_validation(tmp_path: Path) -> None:
    mod = tmp_path / "good_mod"
    mod.mkdir()
    (mod / "manifest.toml").write_text(
        'name = "good_mod"\nversion = "1.2.3"\ndepends = ["web"]\n[load]\ndata = ["data/x.json"]\n'
    )
    with pytest.raises(ModuleError, match="missing file"):
        load_manifest(mod)
    (mod / "data").mkdir()
    (mod / "data" / "x.json").write_text("[]")
    manifest = load_manifest(mod)
    assert manifest.depends == ["core", "web"]  # core is implied
    assert manifest.files("data") == [mod / "data" / "x.json"]
    bad = tmp_path / "BadName"
    bad.mkdir()
    (bad / "manifest.toml").write_text('name = "BadName"\n')
    with pytest.raises(ModuleError):
        load_manifest(bad)


@pytest.mark.db
def test_registry_composition(registry):  # type: ignore[no-untyped-def]
    book = registry.models["ktest.book"]
    assert [f._module for f in book._fragments] == ["kernel_test_a", "kernel_test_b", "chatter_test"]
    assert "core.multi_company" in book._mixin_names
    assert book._fields["company_id"].module == "core"
    assert book._fields["isbn"].module == "kernel_test_b"
    assert book._soft_delete is True and "deleted_at" in book._fields
    assert ("ktest.chapter", "words") in registry.triggers
    trigger = next(t for t in registry.triggers[("ktest.chapter", "words")] if t.field == "total_words")
    assert trigger.path == "chapter_ids"
    assert registry.m2m_tables["ktest_book__tag_ids"] is not None
    assert "ktest.review" in registry.models


@pytest.mark.db
def test_module_state_recorded(env):  # type: ignore[no-untyped-def]
    modules = env["core.module"]
    assert modules.search([("name", "=", "core"), ("state", "=", "installed")])
    kb = modules.search([("name", "=", "kernel_test_b")])
    assert kb.state == "installed" and kb.installed_version == "1.0.0"
    assert kb.depends == ["core", "kernel_test_a"]
    assert env["core.module_record"].search_count([("module", "=", "core"), ("name", "=", "main_company")]) == 1


def test_extension_of_unknown_model_fails() -> None:
    from tessera.orm.model import Model
    from tessera.orm.registry import Registry, forget_module_fragments, register_fragment, set_loading_module

    set_loading_module("broken_mod")
    try:

        class Broken(Model):
            _extend = "ghost.model"
            _register = False

        register_fragment(Broken, "broken_mod")
    finally:
        set_loading_module(None)
    reg = Registry("postgresql://x@localhost/none")
    reg.module_order = ["broken_mod"]
    with pytest.raises(RegistryError, match="unknown model"):
        reg.build()
    forget_module_fragments("broken_mod")
