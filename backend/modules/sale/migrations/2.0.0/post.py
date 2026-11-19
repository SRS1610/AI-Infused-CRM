"""sale 1.x → 2.0.0 (D-074): Phase 1 tax percentages become accounting taxes.

Each distinct non-zero ``tax_percent`` of the quotation lines maps, in the line's company, to an existing sales
tax of that rate or a new one booked on the tax-due account; products with a percentage and no customer tax
get the same treatment (``modules.sale.taxmap``). The line column is then dropped (``down.py`` recreates it
from the taxes) and the totals are recomputed from the taxes. Accepted quotations are sales orders from now
on: they start unlocked, with their prices kept as typed, and their invoicing status is computed.
"""

from __future__ import annotations

from typing import Any

from modules.sale.taxmap import drop_line_tax_percent, map_line_tax_percent, map_product_tax_percent
from tessera.logging import get_logger

log = get_logger(__name__)


def migrate(env: Any, from_version: str) -> None:
    lines = map_line_tax_percent(env)
    products = map_product_tax_percent(env)
    drop_line_tax_percent(env)
    cr = env.cr
    cr.execute("UPDATE sale_quotation SET locked = FALSE WHERE locked IS NULL")
    cr.execute("UPDATE sale_quotation_line SET qty_delivered = 0 WHERE qty_delivered IS NULL")
    cr.execute("UPDATE sale_quotation_line SET qty_returned = 0 WHERE qty_returned IS NULL")
    cr.execute("UPDATE sale_quotation_line SET price_manual = TRUE WHERE price_manual IS NULL")
    cr.execute(
        "UPDATE sale_quotation SET confirmation_date = COALESCE(signed_at, write_date) "
        "WHERE state = 'accepted' AND confirmation_date IS NULL"
    )
    env.cache.invalidate("sale.quotation")
    env.cache.invalidate("sale.quotation_line")
    # columns added by the upgrade hold no value yet: compute every stored computed field of the documents
    for model_name in ("sale.quotation_line", "sale.quotation"):
        model = env[model_name].elevated().with_context(active_test=False)
        ids = model.search([]).ids
        if not ids:
            continue
        for field in model._fields.values():
            if field.recompute_stored:
                env.add_to_compute(model_name, field.name, ids)
    env.flush()
    log.info("sale 2.0.0 tax migration", lines=lines, products=products)
