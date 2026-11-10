"""Database-backed security provider (groups, access rights, record rules, field groups)."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from tessera.orm.domain import And, Leaf, Node, Or, parse, substitute

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.registry import Registry

MULTI_COMPANY_RULE: Node = Or((Leaf("company_id", "is_not_set"), Leaf("company_id", "in", "$companies")))


@dataclass(frozen=True)
class AccessRow:
    group_id: int | None
    perms: dict[str, bool]


@dataclass(frozen=True)
class RuleRow:
    groups: frozenset[int]
    domain: Node
    perms: dict[str, bool]


class DatabaseSecurityProvider:
    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self._lock = threading.RLock()
        self._acl: dict[str, list[AccessRow]] | None = None
        self._rules: dict[str, list[RuleRow]] | None = None
        self._implied: dict[int, set[int]] | None = None
        self._user_groups: dict[int, frozenset[int]] = {}
        self._group_ids_by_xmlid: dict[str, int | None] = {}

    # -- loading ---------------------------------------------------------------------------
    def _ensure_loaded(self, env: Environment) -> None:
        with self._lock:
            if self._acl is not None and self._rules is not None and self._implied is not None:
                return
            cr = env.cr
            acl: dict[str, list[AccessRow]] = {}
            for model_name, group_id, r, w, c, u in cr.fetchall(
                "SELECT model_name, group_id, perm_read, perm_write, perm_create, perm_unlink "
                "FROM core_model_access WHERE active"
            ):
                acl.setdefault(model_name, []).append(
                    AccessRow(group_id, {"read": bool(r), "write": bool(w), "create": bool(c), "unlink": bool(u)})
                )
            rule_groups: dict[int, set[int]] = {}
            for rule_id, group_id in cr.fetchall(
                "SELECT core_record_rule_id, core_group_id FROM core_record_rule__group_ids"
            ):
                rule_groups.setdefault(rule_id, set()).add(group_id)
            rules: dict[str, list[RuleRow]] = {}
            for rule_id, model_name, domain, r, w, c, u in cr.fetchall(
                "SELECT id, model_name, domain, perm_read, perm_write, perm_create, perm_unlink "
                "FROM core_record_rule WHERE active"
            ):
                rules.setdefault(model_name, []).append(
                    RuleRow(
                        frozenset(rule_groups.get(rule_id, set())),
                        parse(domain),
                        {"read": bool(r), "write": bool(w), "create": bool(c), "unlink": bool(u)},
                    )
                )
            implied: dict[int, set[int]] = {}
            for group_id, implied_id in cr.fetchall(
                "SELECT core_group_id, implied_group_id FROM core_group__implied_ids"
            ):
                implied.setdefault(group_id, set()).add(implied_id)
            self._acl, self._rules, self._implied = acl, rules, implied
            self._user_groups.clear()
            self._group_ids_by_xmlid.clear()

    def user_groups(self, env: Environment, uid: int | None = None) -> frozenset[int]:
        uid = uid or env.uid
        self._ensure_loaded(env)
        with self._lock:
            cached = self._user_groups.get(uid)
            if cached is not None:
                return cached
            direct = {
                r[0]
                for r in env.cr.fetchall(
                    "SELECT core_group_id FROM core_user__group_ids WHERE core_user_id = :uid", {"uid": uid}
                )
            }
            closure = set(direct)
            frontier = list(direct)
            implied = self._implied or {}
            while frontier:
                g = frontier.pop()
                for i in implied.get(g, ()):
                    if i not in closure:
                        closure.add(i)
                        frontier.append(i)
            result = frozenset(closure)
            self._user_groups[uid] = result
            return result

    def group_id(self, env: Environment, xmlid: str) -> int | None:
        with self._lock:
            if xmlid in self._group_ids_by_xmlid:
                return self._group_ids_by_xmlid[xmlid]
        module, _, name = xmlid.partition(".")
        row = env.cr.fetchone(
            "SELECT res_id FROM core_module_record WHERE module = :m AND name = :n AND model = 'core.group'",
            {"m": module, "n": name},
        )
        gid = int(row[0]) if row else None
        with self._lock:
            self._group_ids_by_xmlid[xmlid] = gid
        return gid

    # -- protocol ---------------------------------------------------------------------------
    def check_model_access(self, env: Environment, model_name: str, operation: str) -> bool:
        if env.is_system:
            return True
        self._ensure_loaded(env)
        rows = (self._acl or {}).get(model_name, [])
        if not rows:
            return False
        groups = self.user_groups(env)
        return any((row.group_id is None or row.group_id in groups) and row.perms.get(operation, False) for row in rows)

    def rule_domain(self, env: Environment, model_name: str, operation: str) -> Node | None:
        if env.is_system:
            return None
        self._ensure_loaded(env)
        parts: list[Node] = []
        rows = (self._rules or {}).get(model_name, [])
        groups = self.user_groups(env)
        group_rules: list[Node] = []
        for row in rows:
            if not row.perms.get(operation, True):
                continue
            if not row.groups:
                parts.append(row.domain)
            elif row.groups & groups:
                group_rules.append(row.domain)
        if group_rules:
            parts.append(Or(tuple(group_rules)) if len(group_rules) > 1 else group_rules[0])
        cls = self.registry.models.get(model_name)
        if cls is not None and "company_id" in cls._fields and cls._fields["company_id"].store:
            parts.append(MULTI_COMPANY_RULE)
        if not parts:
            return None
        node: Node = parts[0] if len(parts) == 1 else And(tuple(parts))
        return substitute(node, env[model_name]._domain_variables())

    def has_groups(self, env: Environment, group_refs: Iterable[str]) -> bool:
        if env.is_system:
            return True
        groups = self.user_groups(env)
        for ref in group_refs:
            gid = self.group_id(env, ref)
            if gid is not None and gid in groups:
                return True
        return False

    def invalidate(self) -> None:
        with self._lock:
            self._acl = None
            self._rules = None
            self._implied = None
            self._user_groups.clear()
            self._group_ids_by_xmlid.clear()

    def describe(self, env: Environment) -> dict[str, Any]:
        self._ensure_loaded(env)
        return {"models_with_acl": len(self._acl or {}), "models_with_rules": len(self._rules or {})}
