"""product 1.x → 2.0.0: give the existing units a type and a rounding precision.

Phase 1 units only had a ratio (``factor``). Per category, the first active unit of ratio 1 becomes the
reference unit; the others are bigger or smaller than it. A second unit of ratio 1 in the same category
(possible in Phase 1) is classified as bigger and should be reviewed. Products need nothing: Phase 1
products are single products (no template, no attribute values) and the new stored flags default to
"not a template" (D-071).
"""

from __future__ import annotations

from typing import Any


def migrate(env: Any, from_version: str) -> None:
    cr = env.cr
    cr.execute("UPDATE product_uom SET rounding = 0.01 WHERE rounding IS NULL OR rounding <= 0")
    cr.execute(
        """
        WITH reference AS (
            SELECT DISTINCT ON (category) id
              FROM product_uom
             WHERE factor = 1
             ORDER BY category, active DESC, id
        )
        UPDATE product_uom u
           SET uom_type = CASE
                   WHEN u.id IN (SELECT id FROM reference) THEN 'reference'
                   WHEN u.factor < 1 THEN 'smaller'
                   ELSE 'bigger'
               END
         WHERE u.uom_type IS NULL
        """
    )
    # stored flags of existing products: sellable unless not for sale, never a template
    cr.execute("UPDATE product_product SET is_template = FALSE WHERE is_template IS NULL")
    cr.execute(
        "UPDATE product_product SET is_sellable = COALESCE(sale_ok, FALSE) AND NOT is_template "
        "WHERE is_sellable IS NULL"
    )
    cr.execute("UPDATE product_product SET price_extra = 0 WHERE price_extra IS NULL")
    env.cache.invalidate("product.uom")
    env.cache.invalidate("product.product")
