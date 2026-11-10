"""The sandboxed expression language: what works, and every escape route we know of being closed."""

from __future__ import annotations

import datetime as dt

import pytest

from tessera.expr import ExpressionError, evaluate


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("1 + 2 * 3", 7),
        ("'a' + 'b'", "ab"),
        ("x > 3 and x < 10", True),
        ("'big' if x > 100 else 'small'", "small"),
        ("x in [1, 5, 9]", True),
        ("d['k'] + 1", 2),
        ("d['missing']", None),
        ("upper(s)", "HELLO"),
        ("contains(s, 'ELL')", True),
        ("matches(s, '^h.l')", True),
        ("len(items[1:])", 2),
        ("coalesce(None, '', 'x')", "x"),
        ("today + days(3) == date('2026-01-04')", True),
        ("today.year", 2026),
        ("not x", False),
        ("-x", -5),
        ("max(1, x, 3)", 5),
        ("{'a': x}['a']", 5),
    ],
)
def test_evaluates(source: str, expected: object) -> None:
    names = {"x": 5, "d": {"k": 1}, "s": "hello", "items": [1, 2, 3], "today": dt.date(2026, 1, 1)}
    assert evaluate(source, names) == expected


@pytest.mark.parametrize(
    "source",
    [
        "().__class__",
        "x.__class__.__mro__",
        "__import__('os')",
        "open('/etc/passwd')",
        "getattr(x, 'real')",
        "[c for c in 'abc']",
        "lambda: 1",
        "s.upper()",  # method calls are not allowed, only whitelisted functions
        "x.real",  # no attributes on arbitrary objects
        "eval('1')",
        "_secret",
        "(x := 3)",
        "upper(s=1)",
        "'a' * 100000",
        "2 ** 1000",
        "1 / 0",
        "unknown_name",
        "f'{x}'",
        "*items",
    ],
)
def test_rejects(source: str) -> None:
    with pytest.raises(ExpressionError):
        evaluate(source, {"x": 5, "s": "hello", "items": [1, 2], "_secret": 1})


def test_syntax_error_is_readable() -> None:
    with pytest.raises(ExpressionError, match="Syntax error"):
        evaluate("1 +", {})


def test_step_budget() -> None:
    expr = " + ".join(["1"] * 6000)
    with pytest.raises(ExpressionError):
        evaluate(expr, {})


@pytest.mark.db
def test_record_attribute_access(env) -> None:  # type: ignore[no-untyped-def]
    book = env["ktest.book"].create({"name": "Expr", "page_count": 12})
    assert evaluate("record.page_count * 2", {"record": book}) == 24
    assert evaluate("record.name + ' / ' + str(record.status)", {"record": book}) == "Expr / draft"
    assert evaluate("record.id", {"record": book}) == book.ids[0]
    with pytest.raises(ExpressionError):
        evaluate("record.no_such_field", {"record": book})
    with pytest.raises(ExpressionError):
        evaluate("record.env", {"record": book})
