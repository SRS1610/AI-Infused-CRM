"""account_bank hooks: the shipped reconciliation rules for every company with a chart of accounts."""

from __future__ import annotations

from typing import Any

from tessera.orm.hooks import hook


def post_init(env: Any) -> None:
    from modules.account_bank.rules import install_default_models

    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        install_default_models(company)


@hook("after_create")
def rules_for_new_company(records: Any, vals_list: Any) -> None:
    """A company created later gets the rules once its chart is installed (``account`` runs first)."""
    if records._name != "core.company":
        return
    from modules.account_bank.rules import install_default_models

    for company in records.elevated("bank rules"):
        if company.chart_template:
            install_default_models(company)
