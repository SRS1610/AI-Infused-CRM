"""Helpers for analytic tests."""

from __future__ import annotations

from typing import Any

from tessera.orm import Model


def plan(env: Any, name: str = "Departments", **vals: Any) -> Model:
    return env["account.analytic_plan"].create({"name": name, **vals})


def analytic(env: Any, plan_rec: Model, code: str, name: str | None = None, **vals: Any) -> Model:
    return env["account.analytic_account"].create(
        {"code": code, "name": name or code, "plan_id": plan_rec.ids[0], **vals}
    )


def dist(*pairs: tuple[Model, float]) -> dict[str, float]:
    return {str(acc.ids[0]): float(pct) for acc, pct in pairs}


def account(env: Any, code: str, company: Model | None = None) -> Model:
    company = company or env.company
    return env["account.account"].search([("code", "=", code), ("company_id", "=", company.ids[0])], limit=1)
