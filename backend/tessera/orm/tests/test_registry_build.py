"""A registry rebuild (Studio) never exposes a half-built registry to requests running meanwhile."""

from __future__ import annotations

from typing import Any


def test_rebuild_composes_on_a_staging_copy_and_publishes_at_once(registry: Any) -> None:
    seen: list[tuple[int, bool, bool]] = []

    def observe(staging: Any) -> None:
        # called mid-build: the live registry still serves the previous, complete models
        seen.append((len(registry.models), "core.user" in registry.tables, staging is registry))

    before = registry.models["core.user"]
    registry.hooks["before_build"].append(observe)
    try:
        registry.build()
    finally:
        registry.hooks["before_build"].remove(observe)
    assert seen and seen[0][0] > 0 and seen[0][1] and not seen[0][2]
    rebuilt = registry.models["core.user"]
    assert rebuilt is not before and rebuilt._registry is registry
    assert registry.tables["core.user"] is not None and registry.ready
