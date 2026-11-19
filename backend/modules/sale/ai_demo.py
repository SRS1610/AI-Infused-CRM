"""Sales AI demo (``tessera seed demo --phase 2``): answers of the deterministic test provider for the
model-based steps (lines from a pasted request, product description drafts) and a first pricing check of the
demo orders. The discount bands themselves are demo data (``data/demo/discount_bands.json``). The rules only
apply to the demo provider; a real provider ignores them."""

from __future__ import annotations

from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)

DEMO_REQUEST = (
    "Hello, for our new packaging line we would like a quote for: 2 collaborative robot arms, 2 vacuum gripper "
    "kits, one force-torque sensor (HW-FTS) and 3 consulting days for the commissioning in January. "
    "Could you also add a 3D-printed fixture? Thanks, Jordan"
)


def demo_rules() -> list[dict[str, Any]]:
    return [
        {
            "match": {"feature": "sale.quote_from_text", "contains": "packaging line"},
            "respond": {
                "json": {
                    "items": [
                        {"description": "collaborative robot arms", "quantity": 2, "unit": None, "code": None,
                         "constraints": None},
                        {"description": "vacuum gripper kits", "quantity": 2, "unit": None, "code": None,
                         "constraints": None},
                        {"description": "force-torque sensor", "quantity": 1, "unit": None, "code": "HW-FTS",
                         "constraints": None},
                        {"description": "consulting days for the commissioning", "quantity": 3, "unit": "days",
                         "code": None, "constraints": "Commissioning in January"},
                        {"description": "3D-printed fixture", "quantity": 1, "unit": None, "code": None,
                         "constraints": None},
                    ]
                }
            },
        },
        {
            "match": {"feature": "product.description_draft", "contains": "Organic cotton T-shirt"},
            "respond": {
                "json": {
                    "description": "A soft organic cotton T-shirt for everyday wear. It comes in red, blue and "
                    "green, in sizes S, M and L.",
                    "facts_used": ["Name", "Colour", "Size"],
                }
            },
        },
    ]  # fmt: skip


@hook("seed_demo")
def seed_sale_ai(env: Any) -> None:
    provider = env.ref("ai_core.provider_local", raise_if_not_found=False)
    if provider is not None and provider.kind == "mock":
        options = dict(provider.options or {})
        keep = [
            r
            for r in options.get("script", [])
            if not str(r.get("match", {}).get("feature", "")).startswith(("sale.", "product."))
        ]
        options["script"] = keep + demo_rules()
        provider.write({"options": options})
    if "ai_core.feature" not in env.registry.models:
        return
    from modules.sale.ai_pricing import sync_flags

    orders = env["sale.quotation"].search([("client_reference", "like", "DEMO-SO-")])
    for order in orders:
        sync_flags(order, notify=False)
    log.info("sales AI demo", orders_checked=len(orders))
