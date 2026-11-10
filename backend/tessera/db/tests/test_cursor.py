"""Rollback callbacks follow the savepoint they belong to."""

from __future__ import annotations

from typing import Any

import pytest


class _Boom(Exception):
    pass


def test_released_savepoint_hands_its_undo_callbacks_to_the_enclosing_level(cr: Any) -> None:
    calls: list[str] = []
    with cr.savepoint():
        cr.on_rollback(lambda: calls.append("released"))
    # a later sibling savepoint rolls back: the released one's work survives, so does its callback
    with pytest.raises(_Boom), cr.savepoint():
        cr.on_rollback(lambda: calls.append("sibling"))
        raise _Boom
    assert calls == ["sibling"]
    cr.rollback()
    assert calls == ["sibling", "released"]


def test_nested_savepoint_rollback_runs_only_its_own_callbacks(cr: Any) -> None:
    calls: list[str] = []
    cr.on_rollback(lambda: calls.append("outer"))
    with pytest.raises(_Boom), cr.savepoint():
        with cr.savepoint():
            cr.on_rollback(lambda: calls.append("inner"))
        raise _Boom
    assert calls == ["inner"]
    cr.commit()
    cr.rollback()
    assert calls == ["inner"]  # committed work is not undone
