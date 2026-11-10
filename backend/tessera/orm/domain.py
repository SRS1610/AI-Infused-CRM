"""Domain DSL (D-005).

A domain is nested JSON::

    {"all": [["company_id", "in", "$companies"],
             {"any": [["user_id", "=", "$uid"], ["team_id.member_ids.user_id", "=", "$uid"]]},
             {"not": ["stage_id.is_lost", "=", true]}]}

* leaf: ``[path, operator, value]``
* ``{"all": [...]}`` / ``{"any": [...]}`` / ``{"not": node}``
* a bare list of leaves/nodes is an implicit ``all``
* variables: ``$uid``, ``$company``, ``$companies``, ``$now``, ``$today``, ``$user.<field>``

This module parses, normalises, substitutes variables and evaluates domains in memory.
SQL compilation lives in :mod:`tessera.orm.sql`.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from tessera.exceptions import ValidationError

OPERATORS = frozenset(
    {
        "=",
        "!=",
        ">",
        ">=",
        "<",
        "<=",
        "in",
        "not_in",
        "like",
        "ilike",
        "not_like",
        "not_ilike",
        "starts_with",
        "any",
        "not_any",
        "is_set",
        "is_not_set",
        "child_of",
        "parent_of",
    }
)
NEGATION = {
    "=": "!=",
    "!=": "=",
    ">": "<=",
    ">=": "<",
    "<": ">=",
    "<=": ">",
    "in": "not_in",
    "not_in": "in",
    "like": "not_like",
    "not_like": "like",
    "ilike": "not_ilike",
    "not_ilike": "ilike",
    "any": "not_any",
    "not_any": "any",
    "is_set": "is_not_set",
    "is_not_set": "is_set",
}

Variables = Mapping[str, Any]
RawDomain = Any


@dataclass(frozen=True)
class Leaf:
    path: str
    operator: str
    value: Any = None

    @property
    def segments(self) -> list[str]:
        return self.path.split(".")

    def to_json(self) -> list[Any]:
        return [self.path, self.operator, _json_safe(self.value)]


@dataclass(frozen=True)
class And:
    children: tuple[Node, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        return {"all": [c.to_json() for c in self.children]}


@dataclass(frozen=True)
class Or:
    children: tuple[Node, ...] = field(default_factory=tuple)

    def to_json(self) -> dict[str, Any]:
        return {"any": [c.to_json() for c in self.children]}


@dataclass(frozen=True)
class Not:
    child: Node

    def to_json(self) -> dict[str, Any]:
        return {"not": self.child.to_json()}


Node = Leaf | And | Or | Not

TRUE_DOMAIN = And(())
FALSE_DOMAIN = Or(())


def _json_safe(value: Any) -> Any:
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, tuple | set | frozenset):
        return [_json_safe(v) for v in value]
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    if hasattr(value, "_ids"):
        return list(value._ids)
    return value


def _is_leaf(obj: Any) -> bool:
    return (
        isinstance(obj, list | tuple)
        and len(obj) in (2, 3)
        and isinstance(obj[0], str)
        and isinstance(obj[1], str)
        and obj[1] in OPERATORS
    )


def parse(raw: RawDomain) -> Node:
    """Parse any accepted input shape into a normalised node tree."""
    if raw is None or raw is True:
        return TRUE_DOMAIN
    if raw is False:
        return FALSE_DOMAIN
    if isinstance(raw, Leaf | And | Or | Not):
        return raw
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return TRUE_DOMAIN
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Invalid domain JSON: {exc.msg}") from exc
        return parse(raw)
    if isinstance(raw, dict):
        if len(raw) != 1:
            raise ValidationError(f"A domain node must have exactly one key, got {sorted(raw)}")
        key, body = next(iter(raw.items()))
        if key == "all":
            return And(tuple(parse(x) for x in _as_list(body)))
        if key == "any":
            return Or(tuple(parse(x) for x in _as_list(body)))
        if key == "not":
            return Not(parse(body))
        raise ValidationError(f"Unknown domain operator '{key}' (expected all/any/not)")
    if _is_leaf(raw):
        path, op, *rest = raw
        if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)*$", path):
            raise ValidationError(f"Invalid field path '{path}' in domain")
        value = rest[0] if rest else None
        if op in ("is_set", "is_not_set") and rest and value not in (None, True):
            raise ValidationError(f"Operator '{op}' takes no value")
        return Leaf(path, op, value)
    if isinstance(raw, list | tuple):
        children = tuple(parse(x) for x in raw)
        return children[0] if len(children) == 1 else And(children)
    raise ValidationError(f"Cannot parse domain element {raw!r}")


def _as_list(body: Any) -> list[Any]:
    if body is None:
        return []
    if _is_leaf(body) or isinstance(body, dict):
        return [body]
    if isinstance(body, list | tuple):
        return list(body)
    raise ValidationError(f"Expected a list of domain nodes, got {body!r}")


def combine_and(*domains: RawDomain) -> Node:
    children: list[Node] = []
    for d in domains:
        node = parse(d)
        if isinstance(node, And):
            children.extend(node.children)
        else:
            children.append(node)
    return children[0] if len(children) == 1 else And(tuple(children))


def combine_or(*domains: RawDomain) -> Node:
    children: list[Node] = []
    for d in domains:
        node = parse(d)
        if isinstance(node, Or):
            children.extend(node.children)
        else:
            children.append(node)
    return children[0] if len(children) == 1 else Or(tuple(children))


def negate(node: Node) -> Node:
    """Push negation down to leaves where an inverse operator exists."""
    if isinstance(node, Not):
        return node.child
    if isinstance(node, And):
        return Or(tuple(negate(c) for c in node.children))
    if isinstance(node, Or):
        return And(tuple(negate(c) for c in node.children))
    if node.operator in NEGATION:
        return Leaf(node.path, NEGATION[node.operator], node.value)
    return Not(node)


def is_true(node: Node) -> bool:
    return isinstance(node, And) and not node.children


def is_false(node: Node) -> bool:
    return isinstance(node, Or) and not node.children


# -- variables -----------------------------------------------------------------------------


def substitute(node: Node, variables: Variables | Callable[[str], Any]) -> Node:
    """Replace ``$var`` strings (and ``$user.field`` paths) in leaf values."""

    def resolve(name: str) -> Any:
        if callable(variables):
            return variables(name)
        if name in variables:
            return variables[name]
        if name.startswith("user.") and "user" in variables:
            value: Any = variables["user"]
            for part in name.split(".")[1:]:
                value = getattr(value, part)
            if hasattr(value, "_ids"):
                ids = list(value._ids)
                return ids[0] if len(ids) == 1 else ids
            return value
        raise ValidationError(f"Unknown domain variable '${name}'")

    def visit(n: Node) -> Node:
        if isinstance(n, Leaf):
            v = n.value
            if isinstance(v, str) and v.startswith("$"):
                v = resolve(v[1:])
            elif isinstance(v, list):
                v = [resolve(x[1:]) if isinstance(x, str) and x.startswith("$") else x for x in v]
            return Leaf(n.path, n.operator, v)
        if isinstance(n, And):
            return And(tuple(visit(c) for c in n.children))
        if isinstance(n, Or):
            return Or(tuple(visit(c) for c in n.children))
        return Not(visit(n.child))

    return visit(node)


def referenced_fields(node: Node) -> set[str]:
    """First path segment of every leaf (used by caches and automation triggers)."""
    out: set[str] = set()

    def visit(n: Node) -> None:
        if isinstance(n, Leaf):
            out.add(n.segments[0])
        elif isinstance(n, And | Or):
            for c in n.children:
                visit(c)
        else:
            visit(n.child)

    visit(node)
    return out


# -- in-memory evaluation ----------------------------------------------------------------

ValueGetter = Callable[[Any, str], Any]


def _to_set(value: Any) -> set[Any]:
    if value is None or value is False:
        return set()
    if hasattr(value, "_ids"):
        return set(value._ids)
    if isinstance(value, list | tuple | set | frozenset):
        return set(value)
    return {value}


def _like(pattern: str, value: Any, case_insensitive: bool) -> bool:
    if value is None:
        return False
    text = str(value)
    pat = str(pattern)
    if case_insensitive:
        text, pat = text.lower(), pat.lower()
    if "%" not in pat and "_" not in pat:
        return pat in text
    regex = "^" + re.escape(pat).replace("%", ".*").replace("_", ".") + "$"
    return re.match(regex, text, re.S) is not None


def _compare_leaf(op: str, actual: Any, expected: Any) -> bool:
    if op == "=":
        if isinstance(actual, bool) or isinstance(expected, bool):
            return bool(actual) == bool(expected)
        if hasattr(actual, "_ids"):
            return _to_set(actual) == _to_set(expected)
        return actual == expected or (actual in (None, False, "") and expected in (None, False, ""))
    if op == "!=":
        return not _compare_leaf("=", actual, expected)
    if op in (">", ">=", "<", "<="):
        if actual is None or expected is None:
            return False
        return bool(
            {
                ">": actual > expected,
                ">=": actual >= expected,
                "<": actual < expected,
                "<=": actual <= expected,
            }[op]
        )
    if op == "in":
        return bool(_to_set(actual) & _to_set(expected)) if hasattr(actual, "_ids") else actual in _to_set(expected)
    if op == "not_in":
        return not _compare_leaf("in", actual, expected)
    if op in ("like", "not_like", "ilike", "not_ilike"):
        res = _like(expected, actual, case_insensitive=op.endswith("ilike"))
        return res if not op.startswith("not") else not res
    if op == "starts_with":
        return str(actual or "").lower().startswith(str(expected).lower())
    if op == "is_set":
        return actual not in (None, False, "", ()) and not (hasattr(actual, "_ids") and not actual._ids)
    if op == "is_not_set":
        return not _compare_leaf("is_set", actual, expected)
    raise ValidationError(f"Operator '{op}' cannot be evaluated in memory")


def evaluate(node: Node, record: Any, getter: ValueGetter | None = None) -> bool:
    """Evaluate a (variable-free) domain against a record-like object.

    ``getter(record, field_name)`` returns the field value; the default uses ``getattr``.
    Relational traversal follows recordsets (objects exposing ``_ids`` and attributes);
    ``any``/``child_of`` on x2many fields apply to every linked record.
    """
    get = getter or (getattr)

    def leaf(n: Leaf) -> bool:
        return _eval_path(n.segments, n.operator, n.value, record, get)

    def visit(n: Node) -> bool:
        if isinstance(n, Leaf):
            return leaf(n)
        if isinstance(n, And):
            return all(visit(c) for c in n.children)
        if isinstance(n, Or):
            return any(visit(c) for c in n.children)
        return not visit(n.child)

    return visit(node)


def _eval_path(segments: Sequence[str], op: str, expected: Any, record: Any, get: ValueGetter) -> bool:
    if record is None or (hasattr(record, "_ids") and not record._ids):
        return op in ("is_not_set", "!=", "not_in", "not_like", "not_ilike", "not_any")
    if hasattr(record, "_ids") and len(record._ids) > 1:
        # x2many traversal: true if any linked record matches (any-semantics)
        return any(_eval_path(segments, op, expected, rec, get) for rec in record)
    head, *rest = segments
    value = get(record, head)
    if rest:
        return _eval_path(rest, op, expected, value, get)
    if op in ("any", "not_any"):
        sub = parse(expected)
        matched = any(evaluate(sub, rec, get) for rec in _iter_records(value))
        return matched if op == "any" else not matched
    if op in ("child_of", "parent_of"):
        raise ValidationError(f"Operator '{op}' requires SQL evaluation")
    if hasattr(value, "_ids") and op in ("=", "!=", "in", "not_in", "is_set", "is_not_set"):
        ids = list(value._ids)
        if op in ("is_set", "is_not_set"):
            return bool(ids) if op == "is_set" else not ids
        if op in ("=", "!="):
            match = _to_set(ids) & _to_set(expected) != set() if ids else expected in (None, False, [])
            return match if op == "=" else not match
        inter = bool(_to_set(ids) & _to_set(expected))
        return inter if op == "in" else not inter
    return _compare_leaf(op, value, expected)


def _iter_records(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if hasattr(value, "_ids"):
        return list(value)
    if isinstance(value, list | tuple):
        return value
    return (value,)


class Domain:
    """Convenience wrapper used throughout the kernel."""

    __slots__ = ("node",)

    def __init__(self, raw: RawDomain = None) -> None:
        self.node = parse(raw)

    def __and__(self, other: RawDomain) -> Domain:
        return Domain(combine_and(self.node, other.node if isinstance(other, Domain) else other))

    def __or__(self, other: RawDomain) -> Domain:
        return Domain(combine_or(self.node, other.node if isinstance(other, Domain) else other))

    def __invert__(self) -> Domain:
        return Domain(negate(self.node))

    def substitute(self, variables: Variables | Callable[[str], Any]) -> Domain:
        return Domain(substitute(self.node, variables))

    def evaluate(self, record: Any, getter: ValueGetter | None = None) -> bool:
        return evaluate(self.node, record, getter)

    def to_json(self) -> Any:
        return self.node.to_json()

    def fields(self) -> set[str]:
        return referenced_fields(self.node)

    @property
    def is_true(self) -> bool:
        return is_true(self.node)

    @property
    def is_false(self) -> bool:
        return is_false(self.node)

    def __repr__(self) -> str:
        return f"Domain({json.dumps(self.to_json())})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Domain) and other.node == self.node

    def __hash__(self) -> int:
        return hash(json.dumps(self.to_json(), sort_keys=True, default=str))
