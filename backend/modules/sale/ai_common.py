"""Shared helpers of the sales AI features (sub-phase 2.6): kill switches, audit runs for in-process
computations, the order lines the features look at and the order history they learn from.

History is read per company with elevated rights because it feeds aggregate statistics (co-purchase counts,
win rates by discount, a customer's usual discount) — never the content of an order the user cannot open.
Everything that reaches a person (a product, a quotation, a line) is read again as that person.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from tessera.i18n import _

if TYPE_CHECKING:
    from tessera.orm.environment import Environment
    from tessera.orm.model import Model

PROVIDER = "Tessera (in-process)"
HISTORY_DAYS = 730
HISTORY_LIMIT = 20000
WON = ("accepted",)
LOST = ("rejected", "expired")


def ai_installed(env: Environment) -> bool:
    return "ai_core.feature" in env.registry.models


def feature_enabled(env: Environment, name: str, company_id: int | None = None) -> bool:
    if not ai_installed(env):
        return False
    feature = env["ai_core.feature"].elevated().search([("name", "=", name)], limit=1)
    if company_id is None:
        company_id = env.company.ids[0] if env.company else None
    return bool(feature) and bool(feature.enabled_for(company_id))


def ensure_enabled(env: Environment, name: str) -> Model:
    """Kill switches and the internal-user check: ``FeatureDisabled`` (404) / ``AccessError``."""
    from tessera.ai.errors import FeatureDisabled
    from tessera.ai.runtime import check_enabled, get_feature

    if not ai_installed(env):
        raise FeatureDisabled(_("AI is not installed"))
    feature = get_feature(env, name)
    check_enabled(env, feature)
    return feature


def audit_run(
    env: Environment,
    name: str,
    *,
    record: Model | None = None,
    inputs: dict[str, Any] | None = None,
    output: Any = None,
    text: str = "",
    model: str = "",
    latency_ms: int = 0,
) -> Model:
    """An ``ai_core.run`` for a computation that made no provider call (audit trail, feedback, proposals)."""
    feature = env["ai_core.feature"].elevated().search([("name", "=", name)], limit=1)
    company = record.elevated().company_id if record is not None and "company_id" in record._fields else None
    return (
        env["ai_core.run"]
        .elevated("ai run")
        .create(
            {
                "feature_id": feature.ids[0] if feature else None,
                "feature": name,
                "user_id": env.uid,
                "company_id": company.ids[0] if company else (env.company.ids[0] if env.company else None),
                "provider": PROVIDER,
                "model": model,
                "status": "ok",
                "res_model": record._name if record is not None else None,
                "res_id": record.ids[0] if record is not None and record.ids else None,
                "input_ref": {"inputs": json.loads(json.dumps(inputs or {}, default=str))},
                "output_text": text,
                "output_data": json.loads(json.dumps(output, default=str)) if output is not None else None,
                "latency_ms": latency_ms,
                "cost": 0.0,
                "steps": 0,
            }
        )
    )


def product_lines(order: Model) -> Model:
    """The sold product lines of an order (no sections, options, rewards or down payments)."""
    return order.line_ids.filtered(
        lambda line: (
            line.product_id
            and not line.display_type
            and not line.is_optional
            and not line.is_reward_line
            and not line.is_downpayment
            and (line.quantity or 0) > 0
        )
    )


def history_lines(
    env: Environment,
    company_id: int,
    states: tuple[str, ...],
    *,
    extra: list[Any] | None = None,
    exclude_order: int | None = None,
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Product lines of the company's decided orders (last two years), as plain rows, for statistics."""
    since = env.now() - dt.timedelta(days=HISTORY_DAYS)
    domain: list[Any] = [
        ("company_id", "=", company_id),
        ("quotation_id.state", "in", list(states)),
        ("quotation_id.date_order", ">=", since),
        ("product_id", "is_set"),
        ("display_type", "is_not_set"),
        ("is_optional", "=", False),
        ("is_reward_line", "=", False),
        ("is_downpayment", "=", False),
        *(extra or []),
    ]
    if exclude_order:
        domain.append(("quotation_id", "!=", exclude_order))
    lines = env["sale.quotation_line"].elevated("sales statistics")
    rows: list[dict[str, Any]] = lines.search_read(
        domain,
        fields or ["quotation_id", "product_id", "quantity", "discount", "price_unit"],
        limit=HISTORY_LIMIT,
        order="id desc",
    )
    return rows


def m2o_id(value: Any) -> int | None:
    """The id of a many2one value as ``search_read`` returns it."""
    if isinstance(value, list | tuple):
        return int(value[0]) if value else None
    if isinstance(value, dict):
        return int(value["id"]) if value.get("id") else None
    return int(value) if value else None


def sales_managers(env: Environment, company_id: int) -> list[int]:
    from tessera.orm.environment import SYSTEM_UID

    group = env.ref("sale.group_sale_manager").ids[0]
    users = env["core.user"].elevated().search([("company_ids", "in", [company_id]), ("share", "=", False)])
    return [u.ids[0] for u in users if u.ids[0] != SYSTEM_UID and group in u.all_group_ids()]
