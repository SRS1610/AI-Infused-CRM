"""Visibility sandbox: hide the records that existed before a point in time from every search of a
transaction (used by AI evaluations, ``tessera.ai.evals``).

Inside :func:`sandbox`, searches of the chosen models (``search``, ``search_count``, ``read_group`` and
everything built on them, such as record-rule filtered retrieval) only see

* records created after the sandbox started (an evaluation's own corpus), and
* the pre-existing records explicitly kept (for an evaluation: the module or demo records its cases refer
  to by external id, ``$xml:contacts.partner_acme``).

Nothing is written: the scope is a pair (highest id at start, kept ids) per model, attached to the
cursor so every environment of the transaction applies it, elevated ones included. Hand-written SQL
over a sandboxed table adds :func:`sql_filter` to stay inside the scope. Reading a record by id
(``browse``) is not filtered.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tessera.orm.domain import Leaf, Node, combine_or

if TYPE_CHECKING:
    from tessera.db.engine import Cursor
    from tessera.orm.environment import Environment


@dataclass(frozen=True)
class Scope:
    """Records of one model visible in the sandbox: ``id > floor`` or ``id in keep``."""

    floor: int
    keep: tuple[int, ...] = ()

    def node(self) -> Node:
        newer = Leaf("id", ">", self.floor)
        return combine_or(newer, Leaf("id", "in", list(self.keep))) if self.keep else newer

    def visible(self, res_id: int) -> bool:
        return res_id > self.floor or res_id in self.keep


def current(cr: Cursor) -> dict[str, Scope] | None:
    return cr.sandbox


def scope_for(cr: Cursor, model: str) -> Scope | None:
    scopes = current(cr)
    return scopes.get(model) if scopes else None


def sql_filter(cr: Cursor, model: str, column: str = "id") -> str:
    """``" AND (<column> > n OR <column> = ANY(...))"`` for raw SQL over ``model``'s table, or ``""``.

    Only integers are interpolated."""
    scope = scope_for(cr, model)
    if scope is None:
        return ""
    keep = ",".join(str(int(i)) for i in scope.keep)
    kept = f" OR {column} = ANY(ARRAY[{keep}]::integer[])" if keep else ""
    return f" AND ({column} > {int(scope.floor)}{kept})"


@contextmanager
def sandbox(
    env: Environment, models: Iterable[str], keep: Mapping[str, Iterable[int]] | None = None
) -> Iterator[dict[str, Scope]]:
    """Hide the existing records of ``models`` (except the ids in ``keep``) until the block ends."""
    env.flush()
    cr = env.cr
    scopes: dict[str, Scope] = {}
    for name in dict.fromkeys(models):
        cls = env.registry.models.get(name)
        if cls is None or not cls._auto:
            continue
        floor = int(cr.scalar(f'SELECT coalesce(max(id), 0) FROM "{cls._table}"') or 0)  # noqa: S608 - registry table
        kept = sorted({int(i) for i in (keep or {}).get(name, ()) if int(i) <= floor})
        scopes[name] = Scope(floor, tuple(kept))
    previous = current(cr)
    cr.sandbox = {**(previous or {}), **scopes}
    try:
        yield scopes
    finally:
        cr.sandbox = previous
