"""Service functions other modules (and the 2.6 AI features) call.

* ``distribution_for(env, line_vals)`` — the distribution the models propose for a line (deterministic
  baseline the "analytic allocation suggestions" feature compares with, and falls back to).
* ``allocation_context(env, company)`` — the plans and accounts a suggestion may use, so a model can only
  propose existing analytic accounts of the company.
* ``validate_distribution(env, distribution, company)`` — raises ``ValidationError`` for an invalid proposal.
"""

from __future__ import annotations

from typing import Any

from modules.account_analytic.models.analytic import _validate, normalise_distribution
from tessera.orm import Model


def distribution_for(env: Any, line_vals: dict[str, Any]) -> dict[str, float]:
    return env["account.analytic_distribution_model"].distribution_for(line_vals)


def validate_distribution(env: Any, distribution: Any, company: Model | None = None) -> dict[str, float] | None:
    _validate(env, distribution, company)
    return normalise_distribution(distribution)


def allocation_context(env: Any, company: Model | None = None) -> dict[str, Any]:
    company = company or env.company
    plans = env["account.analytic_plan"].search([])
    out: list[dict[str, Any]] = []
    for plan in plans:
        if plan.company_id and plan.company_id.ids != company.ids:
            continue
        accounts = plan.account_ids.filtered(
            lambda a: a.active and (not a.company_id or a.company_id.ids == company.ids)
        )
        out.append(
            {
                "plan_id": plan.ids[0],
                "plan": plan.name,
                "default_applicability": plan.default_applicability,
                "accounts": [
                    {
                        "id": acc.ids[0],
                        "code": acc.code,
                        "name": acc.name,
                        "partner": acc.partner_id.display_name if acc.partner_id else None,
                    }
                    for acc in accounts
                ],
            }
        )
    return {"company_id": company.ids[0], "plans": out}
