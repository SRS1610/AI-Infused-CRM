"""Field conversions and metadata (no database)."""

from __future__ import annotations

import datetime as dt

import pytest

from tessera.exceptions import ValidationError
from tessera.orm import fields as F


def test_char_trim_and_size() -> None:
    f = F.Char(size=5)
    assert f.convert_to_cache("  abc ") == "abc"
    assert f.convert_to_cache(None) is None
    with pytest.raises(ValidationError):
        f.convert_to_cache("toolongvalue")


def test_integer_float_monetary() -> None:
    assert F.Integer().convert_to_cache("42") == 42
    with pytest.raises(ValidationError):
        F.Integer().convert_to_cache("x")
    assert F.Float(digits=2).convert_to_cache(1.005) == 1.01
    assert F.Float(digits=(16, 3)).sql_type == "numeric(16,3)"
    assert F.Monetary().convert_to_cache("12.34567") == 12.3457
    assert F.Monetary(currency_field="cur").describe()["currency_field"] == "cur"


def test_boolean_never_required_error() -> None:
    f = F.Boolean(required=True)
    f.validate(False, None)  # type: ignore[arg-type]
    assert f.convert_to_cache(None) is False


def test_dates() -> None:
    assert F.Date().convert_to_cache("2026-01-31T10:00:00") == dt.date(2026, 1, 31)
    d = F.Datetime().convert_to_cache("2026-01-31T10:00:00")
    assert d == dt.datetime(2026, 1, 31, 10, tzinfo=dt.UTC)
    assert F.Datetime().convert_to_cache("2026-01-31T10:00:00+02:00") == dt.datetime(2026, 1, 31, 8, tzinfo=dt.UTC)
    assert F.Datetime().convert_to_read(d, None) == "2026-01-31T10:00:00+00:00"  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        F.Date().convert_to_cache("not a date")


def test_selection_validation() -> None:
    f = F.Selection([("a", "A"), ("b", "B")])
    f.name = "state"
    f.validate("a", None)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        f.validate("z", None)  # type: ignore[arg-type]


def test_html_sanitized() -> None:
    f = F.Html()
    assert "<script" not in f.convert_to_cache("<p>ok</p><script>alert(1)</script>")
    assert F.Html(sanitize=False).convert_to_cache("<b>x</b>") == "<b>x</b>"


def test_binary_base64_roundtrip() -> None:
    f = F.Binary()
    assert f.convert_to_cache("aGVsbG8=") == b"hello"
    assert f.convert_to_read(b"hello", None) == "aGVsbG8="  # type: ignore[arg-type]


def test_x2many_commands_normalisation() -> None:
    norm = F._X2many.normalize_commands
    assert norm([1, 2]) == [("set", [1, 2])]
    assert norm(None) == [("clear",)]
    assert norm([("add", 3), {"name": "x"}, 4]) == [("add", 3), ("create", {"name": "x"}), ("add", 4)]
    assert norm(("set", [1])) == [("set", [1])]
    with pytest.raises(ValidationError):
        norm(["bad"])


def test_many2many_names() -> None:
    f = F.Many2many("ktest.tag")
    f.name, f.model_name = "tag_ids", "ktest.book"
    assert f.relation == "ktest_book__tag_ids"
    assert f.column1 == "ktest_book_id"
    assert f.column2 == "ktest_tag_id"
    self_ref = F.Many2many("ktest.book")
    self_ref.name, self_ref.model_name = "related_ids", "ktest.book"
    assert self_ref.column2 == "ktest_book_other_id"


def test_field_merge_keeps_earlier_attributes() -> None:
    earlier = F.Char("Title", required=True, index=True)
    later = F.Char(help="More help")
    merged = earlier.merge(later)
    assert isinstance(merged, F.Char)
    assert merged.string == "Title"
    assert merged.required is True
    assert merged.help == "More help"
    assert merged.index is True


def test_store_and_copy_defaults() -> None:
    assert F.Char().store is True
    assert F.Char(compute="_c").store is False
    assert F.Char(compute="_c", store=True).store is True
    assert F.Char(compute="_c").copy is False
    assert F.One2many("x", "y").store is False
    assert F.Char(translate=True).sql_type == "jsonb"
    assert F.Binary(attachment=True).store is False
