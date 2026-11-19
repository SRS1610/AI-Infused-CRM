"""Payment demo scenario: the test provider is switched on in every company, so the portal's *Pay now*
button settles demo invoices immediately; one demo invoice per company is paid online."""

from __future__ import annotations

from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)


def seed_company(env: Any, company: Any) -> Any:
    from modules.payment.hooks import install_providers
    from modules.payment.service import create_invoice_transaction, payable_invoices, start_checkout

    env = env.with_company(company)
    install_providers(company)
    provider = env.ref(f"payment.{company.ids[0]}_provider_test", raise_if_not_found=False)
    if provider is None:
        return None
    if provider.state == "disabled":
        provider.write({"state": "test", "allow_tokenization": True})
    if env["account.move"].elevated().search_count([("company_id", "=", company.ids[0]), ("ref", "=", "DEMO-ONLINE")]):
        return None
    customer = env["contacts.partner"].elevated().search([("is_company", "=", True)], order="id", limit=1)
    if not customer:
        return None
    tax = company.elevated().default_sale_tax_id
    target = env["account.move"].create(
        {
            "move_type": "out_invoice",
            "partner_id": customer.ids[0],
            "invoice_date": env.today(),
            "ref": "DEMO-ONLINE",
            "invoice_line_ids": [
                ("create", {"name": "Online order", "quantity": 1, "price_unit": 250.0, "tax_ids": [("set", tax.ids)]})
            ],
        }
    )
    target.action_post()
    if not payable_invoices(target):
        return None
    tx = create_invoice_transaction(target, provider, tokenize=True)
    start_checkout(tx, return_url=target.portal_url())
    return tx


@hook("seed_demo")
def seed_payments(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        tx = seed_company(env, company)
        if tx is not None:
            log.info("payment demo", company=company.name, reference=tx.reference, state=tx.state)
