"""A small, sandboxed expression language for automations, computed defaults and Studio formulas.

Expressions are Python-syntax *expressions* (no statements) evaluated by walking a whitelisted AST:

* literals, lists, tuples, dicts, sets; ``and``/``or``/``not``; comparisons incl. ``in``; arithmetic;
  conditional expressions (``a if cond else b``); subscripts on lists/dicts/strings;
* names: only the variables passed in (``record``, ``old``, ``user``, ``now``, ``today``, ``payload`` …);
* attribute access only on ORM records (field names, never underscored) and on date/datetime values
  (``year``, ``month``, ``day``, ``hour``, ``minute``, ``weekday``);
* calls only to the functions in ``FUNCTIONS``.

There is no way to reach Python internals: dunder/underscore names are rejected at parse time, there is
no attribute access on arbitrary objects, no comprehension or lambda, and evaluation has a step budget
so a hostile expression cannot loop or allocate unboundedly.
"""

from __future__ import annotations

import ast
import datetime as dt
import operator
import re
from collections.abc import Callable, Mapping
from functools import lru_cache
from typing import Any

from tessera.exceptions import ValidationError

MAX_LENGTH = 2000
MAX_STEPS = 10_000
MAX_SEQUENCE = 10_000
MAX_POWER = 64

_BINOPS: dict[type[ast.operator], Callable[[Any, Any], Any]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type[ast.unaryop], Callable[[Any], Any]] = {
    ast.Not: operator.not_,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}
_COMPARE: dict[type[ast.cmpop], Callable[[Any, Any], Any]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
    ast.Is: operator.is_,
    ast.IsNot: operator.is_not,
}
_DATE_ATTRS = {"year", "month", "day", "hour", "minute", "second", "weekday", "isoformat"}


def _days(n: float) -> dt.timedelta:
    return dt.timedelta(days=n)


def _hours(n: float) -> dt.timedelta:
    return dt.timedelta(hours=n)


def _date(value: Any) -> dt.date | None:
    if value is None or value is False:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _safe_str(value: Any) -> str:
    if hasattr(value, "_ids"):
        return ", ".join(value.mapped("display_name"))
    return "" if value is None else str(value)


def _contains(haystack: Any, needle: Any) -> bool:
    return str(needle).lower() in str(haystack or "").lower()


def _matches(value: Any, pattern: str) -> bool:
    if len(pattern) > 200:
        raise ValidationError("Pattern too long")
    return re.search(pattern, str(value or "")) is not None


FUNCTIONS: dict[str, Callable[..., Any]] = {
    "len": len,
    "str": _safe_str,
    "int": int,
    "float": float,
    "bool": bool,
    "round": round,
    "abs": abs,
    "min": min,
    "max": max,
    "sum": sum,
    "upper": lambda s: str(s or "").upper(),
    "lower": lambda s: str(s or "").lower(),
    "strip": lambda s: str(s or "").strip(),
    "contains": _contains,
    "matches": _matches,
    "days": _days,
    "hours": _hours,
    "date": _date,
    "coalesce": lambda *values: next((v for v in values if v not in (None, False, "")), None),
    "ids": lambda rs: list(getattr(rs, "ids", []) or []),
}


class ExpressionError(ValidationError):
    pass


@lru_cache(maxsize=2048)
def compile_expression(source: str) -> ast.Expression:
    """Parse and validate. Raises ``ExpressionError`` with a user-readable message."""
    if len(source) > MAX_LENGTH:
        raise ExpressionError(f"Expression longer than {MAX_LENGTH} characters")
    try:
        tree = ast.parse(source.strip(), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"Syntax error: {exc.msg} (column {exc.offset})") from exc
    for node in ast.walk(tree):
        if isinstance(node, ast.Name | ast.Attribute):
            name = node.id if isinstance(node, ast.Name) else node.attr
            if name.startswith("_"):
                raise ExpressionError(f"Names starting with '_' are not allowed: {name}")
        elif isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
                raise ExpressionError("Only these functions can be called: " + ", ".join(sorted(FUNCTIONS)))
            if node.keywords:
                raise ExpressionError("Keyword arguments are not supported")
        elif not isinstance(node, _ALLOWED_NODES):
            raise ExpressionError(f"'{type(node).__name__}' is not allowed in expressions")
    return tree


_ALLOWED_NODES = (
    ast.Expression,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Set,
    ast.Dict,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.BinOp,
    ast.Compare,
    ast.IfExp,
    ast.Subscript,
    ast.Slice,
    ast.Load,
    *(_BINOPS.keys()),
    *(_UNARY.keys()),
    *(_COMPARE.keys()),
)


class _Evaluator:
    def __init__(self, names: Mapping[str, Any]) -> None:
        self.names = names
        self.steps = 0

    def eval(self, node: ast.AST) -> Any:
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise ExpressionError("Expression too complex")
        method = getattr(self, f"_{type(node).__name__}", None)
        if method is None:  # pragma: no cover - compile_expression rejects these
            raise ExpressionError(f"'{type(node).__name__}' is not allowed")
        return method(node)

    def _Expression(self, node: ast.Expression) -> Any:  # noqa: N802
        return self.eval(node.body)

    def _Constant(self, node: ast.Constant) -> Any:  # noqa: N802
        return node.value

    def _List(self, node: ast.List) -> Any:  # noqa: N802
        return [self.eval(e) for e in node.elts]

    def _Tuple(self, node: ast.Tuple) -> Any:  # noqa: N802
        return tuple(self.eval(e) for e in node.elts)

    def _Set(self, node: ast.Set) -> Any:  # noqa: N802
        return {self.eval(e) for e in node.elts}

    def _Dict(self, node: ast.Dict) -> Any:  # noqa: N802
        if any(k is None for k in node.keys):
            raise ExpressionError("Dict unpacking is not allowed")
        return {self.eval(k): self.eval(v) for k, v in zip(node.keys, node.values, strict=True)}  # type: ignore[arg-type]

    def _Name(self, node: ast.Name) -> Any:  # noqa: N802
        if node.id in ("True", "False", "None"):
            return {"True": True, "False": False, "None": None}[node.id]
        if node.id not in self.names:
            raise ExpressionError(f"Unknown name '{node.id}'")
        return self.names[node.id]

    def _Attribute(self, node: ast.Attribute) -> Any:  # noqa: N802
        base = self.eval(node.value)
        attr = node.attr
        if hasattr(base, "_fields") and hasattr(base, "_ids"):
            if attr in ("id", "ids", "display_name") or attr in base._fields:
                if attr == "id":
                    return base.ids[0] if len(base.ids) == 1 else None
                return getattr(base, attr)
            raise ExpressionError(f"'{base._name}' has no field '{attr}'")
        if isinstance(base, dt.date) and attr in _DATE_ATTRS:
            value = getattr(base, attr)
            return value() if attr in ("weekday", "isoformat") else value
        if isinstance(base, Mapping) and attr in base:
            return base[attr]
        raise ExpressionError(f"Attribute '{attr}' is not available here")

    def _BoolOp(self, node: ast.BoolOp) -> Any:  # noqa: N802
        if isinstance(node.op, ast.And):
            result: Any = True
            for v in node.values:
                result = self.eval(v)
                if not result:
                    return result
            return result
        result = False
        for v in node.values:
            result = self.eval(v)
            if result:
                return result
        return result

    def _UnaryOp(self, node: ast.UnaryOp) -> Any:  # noqa: N802
        return _UNARY[type(node.op)](self.eval(node.operand))

    def _BinOp(self, node: ast.BinOp) -> Any:  # noqa: N802
        left, right = self.eval(node.left), self.eval(node.right)
        if isinstance(node.op, ast.Pow) and isinstance(right, int | float) and abs(right) > MAX_POWER:
            raise ExpressionError("Exponent too large")
        if isinstance(node.op, ast.Mult):
            for a, b in ((left, right), (right, left)):
                if isinstance(a, str | list | tuple) and isinstance(b, int) and len(a) * b > MAX_SEQUENCE:
                    raise ExpressionError("Result too large")
        try:
            result = _BINOPS[type(node.op)](left, right)
        except ZeroDivisionError as exc:
            raise ExpressionError("Division by zero") from exc
        except TypeError as exc:
            raise ExpressionError(f"Incompatible values: {exc}") from exc
        if isinstance(result, str | list | tuple) and len(result) > MAX_SEQUENCE:
            raise ExpressionError("Result too large")
        return result

    def _Compare(self, node: ast.Compare) -> Any:  # noqa: N802
        left = self.eval(node.left)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = self.eval(comparator)
            try:
                ok = _COMPARE[type(op)](left, right)
            except TypeError as exc:
                raise ExpressionError(f"Cannot compare: {exc}") from exc
            if not ok:
                return False
            left = right
        return True

    def _IfExp(self, node: ast.IfExp) -> Any:  # noqa: N802
        return self.eval(node.body) if self.eval(node.test) else self.eval(node.orelse)

    def _Subscript(self, node: ast.Subscript) -> Any:  # noqa: N802
        base = self.eval(node.value)
        if not isinstance(base, list | tuple | str | Mapping):
            raise ExpressionError("Only lists, strings and dicts can be indexed")
        if isinstance(node.slice, ast.Slice):
            parts = [
                self.eval(p) if p is not None else None for p in (node.slice.lower, node.slice.upper, node.slice.step)
            ]
            return base[slice(*parts)]
        key = self.eval(node.slice)
        try:
            return base[key]
        except (KeyError, IndexError, TypeError):
            return None

    def _Call(self, node: ast.Call) -> Any:  # noqa: N802
        func = FUNCTIONS[node.func.id]  # type: ignore[attr-defined]
        args = [self.eval(a) for a in node.args]
        try:
            return func(*args)
        except ExpressionError:
            raise
        except (TypeError, ValueError) as exc:
            raise ExpressionError(f"{node.func.id}(): {exc}") from exc  # type: ignore[attr-defined]


def evaluate(source: str, names: Mapping[str, Any] | None = None) -> Any:
    """Evaluate ``source`` with the given variables."""
    tree = compile_expression(source)
    return _Evaluator(names or {}).eval(tree)


def standard_names(env: Any, record: Any = None, **extra: Any) -> dict[str, Any]:
    """Variables available to every automation expression."""
    names: dict[str, Any] = {
        "now": env.now(),
        "today": env.today(),
        "user": env.user,
        "uid": env.uid,
    }
    if record is not None:
        names["record"] = record
    names.update(extra)
    return names


__all__ = ["FUNCTIONS", "ExpressionError", "compile_expression", "evaluate", "standard_names"]
