"""Phase 1 tax percentages to accounting taxes (D-074).

Phase 1 stored a plain ``tax_percent`` on quotation lines and products. ``sale`` 2.0 uses ``account.tax``
records: each distinct non-zero percentage becomes, per company, an existing sales tax of that rate (percent,
price excluded, not a group; active ones first) or a new one booked on the company's tax-due account. A zero
percentage means "no tax" and maps to no tax. Used by the 2.0.0 migration (lines and products) and on a fresh
install (products whose placeholder percentage has no tax yet).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from tessera.logging import get_logger
from tessera.orm import Model

log = get_logger(__name__)

LINE_TABLE = "sale_quotation_line"


def _column_exists(env: Any, table: str, column: str) -> bool:
    return bool(
        env.cr.scalar(
            "SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c",
            {"t": table, "c": column},
        )
    )


def sale_tax_for_rate(company: Model, rate: float) -> Model:
    """A sales tax of ``rate`` % in ``company``: an existing one, else a new one on the tax-due account."""
    from modules.account.chart import chart_ref

    env = company.env
    company = company.elevated("tax migration")
    taxes = env["account.tax"].elevated("tax migration").with_context(active_test=False)
    found = taxes.search(
        [
            ("company_id", "=", company.ids[0]),
            ("type_tax_use", "=", "sale"),
            ("amount_type", "=", "percent"),
            ("price_include", "=", False),
            ("amount", "=", float(rate)),
        ],
        order="sequence, id",
    ).filtered(lambda t: not t.children_tax_ids)
    if found:
        active = found.filtered(lambda t: t.active)
        return (active or found)[0]
    account = chart_ref(company, "tax_due")
    default = company.default_sale_tax_id
    name = f"Sales tax {float(rate):g}%"
    if taxes.search_count([("company_id", "=", company.ids[0]), ("type_tax_use", "=", "sale"), ("name", "=", name)]):
        name = f"{name} (Phase 1)"
    reps = [
        (
            "create",
            {
                "document_type": doc,
                "repartition_type": kind,
                "factor_percent": 100.0,
                **({"account_id": account.ids[0]} if kind == "tax" and account else {}),
            },
        )
        for doc in ("invoice", "refund")
        for kind in ("base", "tax")
    ]
    tax = taxes.create(
        {
            "name": name,
            "description": f"{float(rate):g}%",
            "type_tax_use": "sale",
            "amount_type": "percent",
            "amount": float(rate),
            "company_id": company.ids[0],
            "tax_group_id": default.tax_group_id.ids[0] if default and default.tax_group_id else None,
            "repartition_line_ids": reps,
        }
    )
    log.info("sales tax created for a Phase 1 rate", company=company.name, rate=rate, tax=tax.ids[0])
    return tax


def map_line_tax_percent(env: Any) -> int:
    """Give quotation lines the tax of their Phase 1 percentage (while the column exists). Returns the number of
    lines updated."""
    if not _column_exists(env, LINE_TABLE, "tax_percent"):
        return 0
    from modules.sale.models.quotation import INTERNAL

    rows = env.cr.fetchall(
        "SELECT l.id, q.company_id, l.tax_percent FROM sale_quotation_line l "
        "JOIN sale_quotation q ON q.id = l.quotation_id "
        "WHERE l.display_type IS NULL AND COALESCE(l.tax_percent, 0) <> 0"
    )
    groups: dict[tuple[int, float], list[int]] = defaultdict(list)
    for line_id, company_id, rate in rows:
        groups[(int(company_id), float(rate))].append(int(line_id))
    companies = env["core.company"].elevated()
    lines = env["sale.quotation_line"].elevated("tax migration").with_context(**{INTERNAL: True})
    count = 0
    for (company_id, rate), line_ids in sorted(groups.items()):
        tax = sale_tax_for_rate(companies.browse([company_id]), rate)
        lines.browse(line_ids).write({"tax_ids": [("add", tax.ids[0])]})
        count += len(line_ids)
    return count


def map_product_tax_percent(env: Any) -> int:
    """Give products with a Phase 1 percentage and no customer tax in a company the tax of that percentage there
    (the product's company, or every company with a chart for shared products). Variants follow their
    template. Returns the number of products updated."""
    products = env["product.product"].elevated("tax migration").with_context(active_test=False)
    companies = env["core.company"].elevated().search([("chart_template", "!=", None)])
    count = 0
    for product in products.search([("tax_percent", "!=", 0), ("template_id", "is_not_set")]):
        targets = product.company_id if product.company_id else companies
        add: list[int] = []
        for company in targets:
            if not company.chart_template or product._taxes_for(company, "sale"):
                continue
            add.append(sale_tax_for_rate(company, float(product.tax_percent)).ids[0])
        if add:
            product.write({"taxes_id": [("add", tax_id) for tax_id in add]})
            count += 1
    return count


def drop_line_tax_percent(env: Any) -> None:
    env.cr.execute(f'ALTER TABLE "{LINE_TABLE}" DROP COLUMN IF EXISTS tax_percent')


def restore_line_tax_percent(env: Any) -> None:
    """Undo: recreate the column with the sum of each line's percentage taxes."""
    env.cr.execute(f'ALTER TABLE "{LINE_TABLE}" ADD COLUMN IF NOT EXISTS tax_percent double precision DEFAULT 0')
    if not env.cr.scalar("SELECT to_regclass('sale_quotation_line_tax_rel') IS NOT NULL"):
        return
    env.cr.execute(
        f'UPDATE "{LINE_TABLE}" l SET tax_percent = s.rate FROM ('  # noqa: S608 - constant table name
        "SELECT r.line_id, SUM(t.amount) AS rate FROM sale_quotation_line_tax_rel r "
        "JOIN account_tax t ON t.id = r.tax_id WHERE t.amount_type = 'percent' GROUP BY r.line_id"
        ") s WHERE s.line_id = l.id"
    )
