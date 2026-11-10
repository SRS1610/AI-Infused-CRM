"""sale hooks: on a fresh install, products keeping a Phase 1 tax percentage get the matching sales tax, and the
quotations loaded with the module's data take their lines' taxes from the products."""

from __future__ import annotations

from typing import Any


def post_init(env: Any) -> None:
    from modules.sale.taxmap import map_product_tax_percent

    map_product_tax_percent(env)
    lines = (
        env["sale.quotation_line"]
        .elevated()
        .search(
            [("product_id", "is_set"), ("display_type", "is_not_set"), ("quotation_id.state", "in", ["draft", "sent"])]
        )
    )
    lines.filtered(lambda line: not line.tax_ids and not line.is_reward_line)._update_taxes()
