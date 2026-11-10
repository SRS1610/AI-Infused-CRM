"""The security provider the ORM consults on every operation.

The kernel does not know how permissions are stored; the ``core`` module installs a
database-backed provider (groups, ``core.model_access``, ``core.record_rule``). Until then
— i.e. while bootstrapping an empty database — the :class:`PermissiveProvider` allows the
system user and denies everyone else.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from tessera.orm.domain import Node
    from tessera.orm.environment import Environment

Operation = str  # "read" | "write" | "create" | "unlink"
OPERATIONS: tuple[str, ...] = ("read", "write", "create", "unlink")


class SecurityProvider(Protocol):
    def check_model_access(self, env: Environment, model_name: str, operation: Operation) -> bool:
        """Model-level CRUD right for ``env.uid``."""

    def rule_domain(self, env: Environment, model_name: str, operation: Operation) -> Node | None:
        """Record-rule domain (variables already substituted) or ``None`` when unrestricted."""

    def has_groups(self, env: Environment, group_refs: Iterable[str]) -> bool:
        """True if ``env.uid`` belongs to at least one of the given groups (external ids)."""

    def invalidate(self) -> None:
        """Drop cached permission data (called when users/groups/rules change)."""


class PermissiveProvider:
    """System user may do anything; other users may do nothing. Used before ``core`` is loaded."""

    def check_model_access(self, env: Environment, model_name: str, operation: Operation) -> bool:
        return env.is_system

    def rule_domain(self, env: Environment, model_name: str, operation: Operation) -> Node | None:
        return None

    def has_groups(self, env: Environment, group_refs: Iterable[str]) -> bool:
        return env.is_system

    def invalidate(self) -> None:
        return None
