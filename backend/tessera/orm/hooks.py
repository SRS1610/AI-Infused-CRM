"""Registry hooks declared by modules.

::

    from tessera.orm.hooks import hook

    @hook("after_write")
    def track_changes(records, vals, old_values): ...

Hook names: ``registry_built(registry)``, ``before_create(model, vals_list)``,
``after_create(records, vals_list)``, ``before_write(records, vals)``,
``after_write(records, vals, old_values)``, ``before_unlink(records)``,
``after_unlink(model, ids)``, ``module_installed(env, module)``, ``module_upgraded(env, module)``,
``module_uninstalled(env, module)``.

Hooks are attached to the owning module (the one whose code is being loaded) and only
active in registries where that module is installed.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any, TypeVar

from tessera.orm.registry import current_loading_module

F = TypeVar("F", bound=Callable[..., Any])

_module_hooks: dict[str, list[tuple[str, Callable[..., Any]]]] = defaultdict(list)


def hook(name: str) -> Callable[[F], F]:
    def decorator(func: F) -> F:
        module = current_loading_module() or _module_from_qualname(func.__module__)
        _module_hooks[module].append((name, func))
        return func

    return decorator


def _module_from_qualname(qualname: str) -> str:
    parts = qualname.split(".")
    if parts[0] == "modules" and len(parts) > 1:
        return parts[1]
    if "modules" in parts:
        idx = parts.index("modules")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return "kernel"


def hooks_for(module: str) -> list[tuple[str, Callable[..., Any]]]:
    return list(_module_hooks.get(module, ()))
