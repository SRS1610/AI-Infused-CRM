"""sale 2.0.0 → 1.x: recreate the quotation lines' ``tax_percent`` column from their percentage taxes (the taxes
themselves, reused or created by the upgrade, are kept)."""

from __future__ import annotations

from typing import Any

from modules.sale.taxmap import restore_line_tax_percent


def migrate(env: Any, from_version: str) -> None:
    restore_line_tax_percent(env)
