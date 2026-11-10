"""E-invoicing demo scenario: Peppol addresses for the EU and US demo companies, the EU company's bank account,
and one invoice sent from the EU company to the US company over the mock access point — delivered, then received
by the US company as a draft vendor bill (2-h)."""

from __future__ import annotations

from typing import Any

from tessera.logging import get_logger
from tessera.orm.hooks import hook

log = get_logger(__name__)
EU_VAT = "FR23334175221"
EU_IBAN = "FR7630006000011234567890189"


def gln(base12: str) -> str:
    """A GS1 GLN with its check digit (13 digits)."""
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(base12))
    return base12 + str((10 - total % 10) % 10)


def setup_companies(env: Any) -> tuple[Any, Any] | None:
    companies = env["core.company"].elevated()
    eu = companies.search([("chart_template", "=", "eu")], limit=1)
    us = env.ref("core.main_company").elevated()
    if not eu or not us or eu.ids == us.ids:
        return None
    if not eu.peppol_endpoint:
        eu.write(
            {
                "vat": eu.vat or EU_VAT,
                "peppol_eas": "9957",
                "peppol_endpoint": eu.vat or EU_VAT,
                "street": eu.street or "12 rue de la Paix",
                "city": eu.city or "Paris",
                "zip": eu.zip or "75002",
                "company_registry": "334175221",
            }
        )
    if not us.peppol_endpoint:
        us.write({"peppol_eas": "0088", "peppol_endpoint": gln("579000043597")})
    journal = (
        env["account.journal"]
        .elevated()
        .search([("company_id", "=", eu.ids[0]), ("type", "=", "bank")], order="sequence, id", limit=1)
    )
    if journal and not journal.bank_account_id:
        holder = env["contacts.partner"].elevated().search([("name", "=", eu.name)], limit=1) or env[
            "contacts.partner"
        ].elevated().create({"name": eu.name, "is_company": True, "company_id": eu.ids[0]})
        account = (
            env["account.bank_account"]
            .elevated()
            .create({"partner_id": holder.ids[0], "acc_number": EU_IBAN, "bic": "BNPAFRPP", "company_id": eu.ids[0]})
        )
        journal.write({"bank_account_id": account.ids[0]})
    return eu, us


def seed(env: Any) -> dict[str, Any] | None:
    from modules.account_einvoice.edi import poll, send_moves

    pair = setup_companies(env)
    if pair is None:
        return None
    eu, us = pair
    moves = env["account.move"].elevated()
    if moves.search_count([("company_id", "=", eu.ids[0]), ("ref", "=", "DEMO-PEPPOL")]):
        return None
    partners = env["contacts.partner"].elevated()
    customer = partners.search([("peppol_endpoint", "=", us.peppol_endpoint)], limit=1) or partners.create(
        {
            "name": us.name,
            "is_company": True,
            "email": us.email or "accounts@my-company.example",
            "country_id": us.country_id.ids[0] if us.country_id else None,
            "street": us.street or "1 Market Street",
            "city": us.city or "San Francisco",
            "zip": us.zip or "94105",
            "peppol_eas": us.peppol_eas,
            "peppol_endpoint": us.peppol_endpoint,
        }
    )
    e = env.with_company(eu)
    tax = eu.default_sale_tax_id
    move = e["account.move"].create(
        {
            "move_type": "out_invoice",
            "partner_id": customer.ids[0],
            "company_id": eu.ids[0],
            "ref": "DEMO-PEPPOL",
            "invoice_line_ids": [
                (
                    "create",
                    {
                        "name": "Robot arm integration",
                        "quantity": 2,
                        "price_unit": 1250.0,
                        "discount": 10,
                        "tax_ids": [("set", tax.ids)],
                    },
                ),
                (
                    "create",
                    {
                        "name": "Remote support (hours)",
                        "quantity": 6,
                        "price_unit": 95.0,
                        "tax_ids": [("set", tax.ids)],
                    },
                ),
            ],
        }
    )
    move.action_post()
    docs = send_moves(move)
    poll(eu)
    received = poll(us)
    return {"invoice": move.name, "state": docs[:1].state, "received": received["received"]}


@hook("seed_demo")
def seed_einvoice(env: Any) -> None:
    result = seed(env)
    if result:
        log.info("e-invoicing demo", **result)
