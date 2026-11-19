"""Module discovery and code loading.

Modules are directories under the configured module paths (``TESSERA_MODULE_PATHS``,
default ``backend/modules``). Code is imported as ``modules.<name>`` (the first path) or,
for extra paths, through a dedicated import prefix registered on ``sys.path``.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType

from tessera.config import DEFAULT_MODULE_PATH, get_settings
from tessera.exceptions import ModuleError
from tessera.logging import get_logger
from tessera.modules.manifest import Manifest, load_manifest
from tessera.orm.registry import Registry, set_loading_module

log = get_logger(__name__)

_manifest_cache: dict[Path, dict[str, Manifest]] = {}


def discover(paths: Iterable[Path] | None = None, *, refresh: bool = False) -> dict[str, Manifest]:
    """Return every module manifest found under ``paths`` (first occurrence wins)."""
    paths = list(paths) if paths is not None else list(get_settings().module_paths)
    found: dict[str, Manifest] = {}
    for base in paths:
        base = Path(base)
        if not base.is_dir():
            continue
        if refresh or base not in _manifest_cache:
            manifests: dict[str, Manifest] = {}
            for child in sorted(base.iterdir()):
                if child.is_dir() and (child / "manifest.toml").is_file():
                    manifests[child.name] = load_manifest(child)
            _manifest_cache[base] = manifests
        for name, manifest in _manifest_cache[base].items():
            found.setdefault(name, manifest)
    return found


def clear_discovery_cache() -> None:
    _manifest_cache.clear()


def import_prefix_for(module_path: Path) -> str:
    """Python import prefix for modules living under ``module_path``."""
    base = module_path.resolve()
    if base == DEFAULT_MODULE_PATH.resolve():
        return "modules"
    prefix = "tessera_ext_" + "".join(ch if ch.isalnum() else "_" for ch in str(base)).strip("_")
    parent = str(base.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    # make <prefix> an alias package for the directory
    if prefix not in sys.modules:
        pkg = ModuleType(prefix)
        pkg.__path__ = [str(base)]
        sys.modules[prefix] = pkg
    return prefix


def import_module_code(manifest: Manifest) -> ModuleType:
    """Import ``<prefix>.<name>`` and every submodule of its ``models`` package."""
    prefix = import_prefix_for(manifest.path.parent)
    qualname = f"{prefix}.{manifest.name}"
    set_loading_module(manifest.name)
    try:
        pkg = importlib.import_module(qualname)
        for sub in ("models", "hooks", "tools"):
            sub_path = manifest.path / sub
            if sub_path.is_dir() and (sub_path / "__init__.py").is_file():
                subpkg = importlib.import_module(f"{qualname}.{sub}")
                for info in pkgutil.iter_modules(subpkg.__path__):
                    importlib.import_module(f"{qualname}.{sub}.{info.name}")
            elif (manifest.path / f"{sub}.py").is_file():
                importlib.import_module(f"{qualname}.{sub}")
    except ModuleError:
        raise
    except Exception as exc:
        raise ModuleError(f"Failed to import module '{manifest.name}': {exc}") from exc
    finally:
        set_loading_module(None)
    return pkg


def load_module_code(registry: Registry, manifest: Manifest) -> None:
    """Import a module's code and register it in the registry's load order."""
    if manifest.name in registry.module_order:
        return
    import_module_code(manifest)
    registry.module_order.append(manifest.name)
    from tessera.orm.hooks import hooks_for

    for hook_name, func in hooks_for(manifest.name):
        registry.register_hook(hook_name, func)
    log.debug("module code loaded", module=manifest.name)
