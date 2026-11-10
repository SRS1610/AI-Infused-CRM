"""A quotation accepted by the customer wins its opportunity (1.4-m)."""

from __future__ import annotations

from typing import Any

from tessera.orm import Model
from tessera.orm.hooks import hook


@hook("after_write")
def quotation_accepted_wins_opportunity(records: Model, vals: dict[str, Any], old_values: dict[Any, Any]) -> None:
    if records._name != "sale.quotation" or vals.get("state") != "accepted":
        return
    for quote in records.elevated("quotation accepted"):
        opp = quote.opportunity_id
        if opp and opp.active and not opp.is_won:
            opp.action_set_won()
            if not opp.expected_revenue:
                opp.write({"expected_revenue": quote.amount_untaxed})
