"""sale_rental hooks."""

from __future__ import annotations

from typing import Any


def post_init(env: Any) -> None:
    """The exclusion constraint against double bookings needs ``btree_gist``: the installer creates it with the
    other extensions before the schema is synchronised; this makes sure it is there on databases prepared by hand."""
    env.cr.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
