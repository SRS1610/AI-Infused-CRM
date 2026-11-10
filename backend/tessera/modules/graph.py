"""Dependency graph resolution (topological order, stable by name)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from tessera.exceptions import ModuleError
from tessera.modules.manifest import Manifest


def closure(manifests: Mapping[str, Manifest], names: Iterable[str]) -> set[str]:
    """All modules needed to install ``names`` (including themselves)."""
    out: set[str] = set()
    stack = list(names)
    while stack:
        name = stack.pop()
        if name in out:
            continue
        manifest = manifests.get(name)
        if manifest is None:
            raise ModuleError(f"Unknown module '{name}'")
        if not manifest.installable:
            raise ModuleError(f"Module '{name}' is not installable")
        out.add(name)
        stack.extend(manifest.depends)
    return out


def resolve_order(manifests: Mapping[str, Manifest], names: Iterable[str]) -> list[str]:
    """Topologically sorted closure of ``names``; ties broken alphabetically (Kahn's algorithm)."""
    wanted = closure(manifests, names)
    indegree: dict[str, int] = dict.fromkeys(wanted, 0)
    dependants: dict[str, list[str]] = {n: [] for n in wanted}
    for name in wanted:
        for dep in manifests[name].depends:
            indegree[name] += 1
            dependants[dep].append(name)
    ready = sorted(n for n, d in indegree.items() if d == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for dep in dependants[current]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                ready.append(dep)
                ready.sort()
    if len(order) != len(wanted):
        cycle = sorted(n for n, d in indegree.items() if d > 0)
        raise ModuleError(f"Dependency cycle among modules: {', '.join(cycle)}")
    return order


def dependants_of(manifests: Mapping[str, Manifest], name: str, installed: Iterable[str]) -> list[str]:
    """Installed modules that (transitively) depend on ``name``."""
    installed_set = set(installed)
    out: set[str] = set()
    changed = True
    targets = {name}
    while changed:
        changed = False
        for mod in installed_set:
            if mod in out or mod == name:
                continue
            if any(dep in targets for dep in manifests[mod].depends):
                out.add(mod)
                targets.add(mod)
                changed = True
    return sorted(out)


def auto_install_candidates(manifests: Mapping[str, Manifest], installed: Iterable[str]) -> list[str]:
    """Modules flagged ``auto_install`` whose dependencies are all installed but which are not."""
    installed_set = set(installed)
    return sorted(
        m.name
        for m in manifests.values()
        if m.auto_install
        and m.installable
        and m.name not in installed_set
        and all(d in installed_set for d in m.depends)
    )
