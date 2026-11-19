"""Manifest loaders for ``views``, ``menus`` and ``actions`` files (registered on the registry)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tessera.exceptions import ModuleError
from tessera.modules.data import DataLoader, qualify
from tessera.modules.manifest import Manifest
from tessera.orm.hooks import hook

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.registry import Registry

VIEW_META_KEYS = {"id", "model", "type", "name", "priority", "inherit", "operations", "active", "sequence", "noupdate"}


@contextmanager
def _entry_context(path: Path, index: int, entry: dict[str, Any]) -> Iterator[None]:
    try:
        yield
    except ModuleError:
        raise
    except Exception as exc:
        raise ModuleError(f"{path.name}[{index}] ({entry.get('id', '?')}): {exc}") from exc


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ModuleError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ModuleError(f"{path} must contain a list")
    return data


def load_views(env: Environment, manifest: Manifest, path: Path, mode: str) -> None:
    loader = DataLoader(env, manifest.name, mode=mode)
    for index, entry in enumerate(_read(path)):
        with _entry_context(path, index, entry):
            _load_views_entry(env, manifest, loader, entry)


def _load_views_entry(env: Environment, manifest: Manifest, loader: DataLoader, entry: dict[str, Any]) -> None:
    if "inherit" in entry:
        values: dict[str, Any] = {
            "name": entry.get("name") or f"{entry['inherit']} (ext by {manifest.name})",
            "inherit_id": entry["inherit"],
            "operations": entry.get("operations", []),
            "sequence": entry.get("sequence", 10),
            "module": manifest.name,
            "active": entry.get("active", True),
        }
        parent = env.ref(qualify(entry["inherit"], manifest.name))
        assert parent is not None
        values["model_name"] = parent.model_name
        values["type"] = parent.type
    else:
        definition = entry.get("definition") or {k: v for k, v in entry.items() if k not in VIEW_META_KEYS}
        if not entry.get("model") or not entry.get("type"):
            raise ModuleError(f"View entry {entry.get('id')} needs model and type")
        values = {
            "name": entry.get("name") or f"{entry['model']}.{entry['type']}",
            "model_name": entry["model"],
            "type": entry["type"],
            "priority": entry.get("priority", 16),
            "definition": definition,
            "module": manifest.name,
            "active": entry.get("active", True),
        }
    loader.load_entry(
        {"id": entry["id"], "model": "web.view", "values": values, "noupdate": entry.get("noupdate", False)}
    )


def load_actions(env: Environment, manifest: Manifest, path: Path, mode: str) -> None:
    loader = DataLoader(env, manifest.name, mode=mode)
    for index, entry in enumerate(_read(path)):
        with _entry_context(path, index, entry):
            _load_actions_entry(env, manifest, loader, entry)


def _load_actions_entry(env: Environment, manifest: Manifest, loader: DataLoader, entry: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "name": entry["name"],
        "kind": entry.get("kind", "window"),
        "model_name": entry.get("model"),
        "view_types": entry.get("views", ["list", "form"]),
        "view_refs": {k: qualify(v, manifest.name) for k, v in (entry.get("view_refs") or {}).items()},
        "search_view_ref": qualify(entry["search_view"], manifest.name) if entry.get("search_view") else None,
        "domain": entry.get("domain"),
        "context": entry.get("context", {}),
        "target": entry.get("target", "current"),
        "url": entry.get("url"),
        "client_tag": entry.get("client_tag"),
        "server_method": entry.get("method"),
        "report_ref": entry.get("report"),
        "limit": entry.get("limit", 80),
        "group_ids": entry.get("groups", []),
        "help": entry.get("help"),
        "sequence": entry.get("sequence", 10),
    }
    loader.load_entry(
        {"id": entry["id"], "model": "web.action", "values": values, "noupdate": entry.get("noupdate", False)}
    )


def load_menus(env: Environment, manifest: Manifest, path: Path, mode: str) -> None:
    loader = DataLoader(env, manifest.name, mode=mode)
    for index, entry in enumerate(_read(path)):
        with _entry_context(path, index, entry):
            _load_menus_entry(env, manifest, loader, entry)


def _load_menus_entry(env: Environment, manifest: Manifest, loader: DataLoader, entry: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "name": entry["name"],
        "parent_id": entry.get("parent"),
        "sequence": entry.get("sequence", 10),
        "action_id": entry.get("action"),
        "group_ids": entry.get("groups", []),
        "icon": entry.get("icon"),
        "color": entry.get("color"),
        "active": entry.get("active", True),
    }
    loader.load_entry(
        {"id": entry["id"], "model": "web.menu", "values": values, "noupdate": entry.get("noupdate", False)}
    )


@hook("registry_built")
def register_loaders(registry: Registry) -> None:
    registry.data_loaders["views"] = load_views
    registry.data_loaders["actions"] = load_actions
    registry.data_loaders["menus"] = load_menus
