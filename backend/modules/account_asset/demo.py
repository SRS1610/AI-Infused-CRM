"""Asset demo (``tessera seed demo --phase 2``): three asset models and three running assets per company with a
chart — a laptop bought on a vendor bill (the computer model is attached to the fixed-asset account, so
posting the bill creates the asset), a vehicle on a declining-then-straight-line board and office furniture
with a salvage value — whose due depreciation entries are posted. Idempotent."""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account.chart import chart_ref
from modules.account_asset.board import add_months
from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
MODELS = {
    "computers": {
        "name": "Computer equipment (3 years)",
        "method": "linear",
        "method_number": 36,
        "method_period": "1",
        "prorata_computation_type": "daily_computation",
    },
    "vehicles": {
        "name": "Vehicles (5 years, declining 40 %)",
        "method": "degressive_then_linear",
        "method_number": 5,
        "method_period": "12",
        "method_progress_factor": 0.4,
        "prorata_computation_type": "none",
    },
    "furniture": {
        "name": "Office furniture (10 years)",
        "method": "linear",
        "method_number": 10,
        "method_period": "12",
        "prorata_computation_type": "constant_periods",
    },
}


def _models(env: Any, company: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    assets = env["account.asset"].elevated()
    for key, vals in MODELS.items():
        model = assets.search(
            [("state", "=", "model"), ("name", "=", vals["name"]), ("company_id", "=", company.ids[0])], limit=1
        )
        if not model:
            accounts = {
                "account_asset_id": chart_ref(company, "fixed_assets"),
                "account_depreciation_id": chart_ref(company, "accumulated_depreciation"),
                "account_depreciation_expense_id": chart_ref(company, "depreciation"),
                "journal_id": chart_ref(company, "journal_misc"),
            }
            model = assets.create(
                {
                    **vals,
                    "state": "model",
                    "company_id": company.ids[0],
                    **{k: v.ids[0] for k, v in accounts.items() if v is not None},
                }
            )
        out[key] = model
    return out


def seed_company(env: Any, company: Any) -> int:
    env = env.with_company(company)
    assets = env["account.asset"]
    if assets.search_count([("company_id", "=", company.ids[0]), ("name", "like", "(demo)")]):
        return 0
    if chart_ref(company, "fixed_assets") is None:
        return 0
    models = _models(env, company)
    today = env.today()
    fixed = chart_ref(company, "fixed_assets").elevated()
    if fixed.create_asset == "no":
        fixed.write({"create_asset": "draft", "asset_model_id": models["computers"].ids[0]})
    vendor = env["contacts.partner"].search([("is_company", "=", True)], limit=1)
    created = assets.browse([])
    bought = add_months(today.replace(day=1), -14) + dt.timedelta(days=14)
    if vendor:
        bill = env["account.move"].create(
            {
                "move_type": "in_invoice",
                "partner_id": vendor.ids[0],
                "invoice_date": bought,
                "date": bought,
                "ref": "DEMO-ASSET-01",
                "invoice_line_ids": [
                    (
                        "create",
                        {
                            "name": "Laptop (demo)",
                            "account_id": fixed.ids[0],
                            "quantity": 1,
                            "price_unit": 2400.0,
                            "tax_ids": [("set", [])],
                        },
                    )
                ],
            }
        )
        bill.action_post()
        created |= assets.search([("original_move_line_ids", "in", bill.line_ids.ids)])
    vehicle_date = dt.date(today.year - 2, 1, 10)
    furniture_date = dt.date(today.year - 3, 4, 1)
    created |= assets.create(
        [
            {
                "name": "Delivery van (demo)",
                "model_id": models["vehicles"].ids[0],
                "original_value": 30000.0,
                "salvage_value": 3000.0,
                "acquisition_date": vehicle_date,
                "company_id": company.ids[0],
            },
            {
                "name": "Office furniture (demo)",
                "model_id": models["furniture"].ids[0],
                "original_value": 6000.0,
                "salvage_value": 500.0,
                "acquisition_date": furniture_date,
                "company_id": company.ids[0],
            },
        ]
    )
    for asset in created:
        if asset.state == "draft":
            asset.action_validate()
        asset.action_post_due(today.isoformat())
    return len(created)


@hook("seed_demo")
def seed_assets(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        count = seed_company(env, company)
        log.info("asset demo", company=company.name, assets=count)
