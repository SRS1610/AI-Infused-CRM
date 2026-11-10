"""payment hooks: a (disabled) Stripe and test provider for every company with a chart of accounts."""

from __future__ import annotations

from typing import Any

from tessera.orm.hooks import hook


def install_providers(company: Any) -> Any:
    """Create the company's providers once (idempotent); they start disabled."""
    env = company.env
    providers = env["payment.provider"].elevated()
    created = providers.browse([])
    cid = company.ids[0]
    journal = (
        env["account.journal"]
        .elevated()
        .search([("company_id", "=", cid), ("type", "=", "bank")], order="sequence, id", limit=1)
    )
    for code, name, sequence in (("stripe", "Stripe", 10), ("test", "Test payments", 90)):
        xmlid = f"payment.{cid}_provider_{code}"
        if env.ref(xmlid, raise_if_not_found=False):
            continue
        record = providers.create(
            {
                "name": name,
                "code": code,
                "state": "disabled",
                "sequence": sequence,
                "company_id": cid,
                "journal_id": journal.ids[0] if journal else None,
                "display_as": "Card" if code == "stripe" else "Test card (no real payment)",
            }
        )
        env.set_ref(xmlid, record, noupdate=True)
        created |= record
    return created


def post_init(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        install_providers(company)


@hook("after_create")
def providers_for_new_company(records: Any, vals_list: Any) -> None:
    if records._name != "core.company":
        return
    for company in records.elevated("payment providers"):
        if company.chart_template:
            install_providers(company)
