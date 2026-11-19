"""Method decorators used in model definitions.

::

    @api.depends("partner_id.name", "ref")
    def _compute_display_name(self): ...

    @api.constrains("probability")
    def _check_probability(self): ...

    @api.expose                      # callable through REST/JSON-RPC `call`
    def action_set_won(self): ...

    @api.onchange("partner_id")
    def _onchange_partner(self): ...
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def depends(*paths: str) -> Callable[[F], F]:
    """Declare the field paths a compute method depends on."""

    def decorator(func: F) -> F:
        func._depends = tuple(paths)  # type: ignore[attr-defined]
        return func

    return decorator


def constrains(*field_names: str) -> Callable[[F], F]:
    """Run the method after create/write when any of the named fields changed."""

    def decorator(func: F) -> F:
        func._constrains = tuple(field_names)  # type: ignore[attr-defined]
        return func

    return decorator


def onchange(*field_names: str) -> Callable[[F], F]:
    """Client-side onchange: the method receives a pseudo-record and may set other fields."""

    def decorator(func: F) -> F:
        func._onchange = tuple(field_names)  # type: ignore[attr-defined]
        return func

    return decorator


def expose(func: F) -> F:
    """Allow the method to be called from the API (`POST /api/v1/<model>/{id}/call/<method>`)."""
    func._exposed = True  # type: ignore[attr-defined]
    return func


def model(func: F) -> F:
    """Marker: the method does not use ``self``'s records (called on an empty recordset)."""
    func._model_method = True  # type: ignore[attr-defined]
    return func


def model_create(func: F) -> F:
    """Marker for ``create`` overrides that accept a list of values dicts."""
    func._model_create = True  # type: ignore[attr-defined]
    return func
