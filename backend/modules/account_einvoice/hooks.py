"""account_einvoice hooks: UBL tax categories on the charts' zero-rate taxes."""

from __future__ import annotations

from typing import Any

from tessera.orm.hooks import hook

# chart template tax id → (UNCL5305 category, VATEX reason code)
CHART_CATEGORIES = {
    "sale_vat_0_eu": ("K", "VATEX-EU-IC"),
    "sale_vat_0_export": ("G", "VATEX-EU-G"),
    "purchase_vat_20_rc": ("AE", "VATEX-EU-AE"),
    "purchase_vat_0_import": ("G", "VATEX-EU-G"),
    "sale_exempt": ("E", None),
    "purchase_exempt": ("E", None),
    "sale_tax_0": ("Z", None),
    "purchase_tax_0": ("Z", None),
}


def tag_chart_taxes(company: Any) -> int:
    from modules.account.chart import chart_ref

    done = 0
    for template_id, (category, code) in CHART_CATEGORIES.items():
        tax = chart_ref(company, template_id)
        if tax is None or tax.edi_tax_category:
            continue
        tax.elevated().write({"edi_tax_category": category, "edi_exemption_reason_code": code})
        done += 1
    return done


def post_init(env: Any) -> None:
    for company in env["core.company"].elevated().search([("chart_template", "!=", None)]):
        tag_chart_taxes(company)


@hook("after_create")
def categories_for_new_company(records: Any, vals_list: Any) -> None:
    if records._name != "core.company":
        return
    for company in records.elevated("e-invoice tax categories"):
        if company.chart_template:
            tag_chart_taxes(company)
