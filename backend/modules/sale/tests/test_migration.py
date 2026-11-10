"""Phase 1 → 2 upgrade of quotations (U-3, 2-l): ``tax_percent`` becomes taxes, totals follow, the column goes and
``down.py`` brings it back."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from modules.account.chart import chart_ref
from modules.account.tests.common import eu_env
from modules.sale.tests.common import customer, order

pytestmark = pytest.mark.db

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations" / "2.0.0"


def _script(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"sale_migration_test_{name}", MIGRATIONS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_column(env: Any) -> bool:
    return bool(
        env.cr.scalar(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = 'sale_quotation_line' AND column_name = 'tax_percent'"
        )
    )


def test_phase1_tax_percent_becomes_taxes(env: Any) -> None:
    assert not _has_column(env)
    # a Phase 1-shaped database: plain percentages on lines and on a product, no taxes
    q = order(
        env,
        customer(env, "Phase 1 buyer"),
        [
            {"name": "Robot work", "price_unit": 100, "quantity": 2},
            {"name": "Parts", "price_unit": 50, "quantity": 1},
            {"name": "Freight", "price_unit": 10, "quantity": 1},
            {"display_type": "section", "name": "Notes"},
        ],
    )
    q.action_confirm()
    eu = eu_env(env)
    eu_quote = order(eu, customer(eu, "Phase 1 Paris"), [{"name": "Conseil", "price_unit": 100, "quantity": 1}])
    old_product = env["product.product"].create({"name": "Phase 1 product", "list_price": 10, "tax_percent": 7})
    old_product.write({"taxes_id": [("set", [])]})
    work, parts, freight = (q.line_ids.filtered(lambda r, n=n: r.name == n) for n in ("Robot work", "Parts", "Freight"))
    env.cr.execute("ALTER TABLE sale_quotation_line ADD COLUMN tax_percent double precision DEFAULT 0")
    env.cr.execute(
        "UPDATE sale_quotation_line SET tax_percent = CASE id WHEN :w THEN 19.6 WHEN :p THEN 6 WHEN :e THEN 20 "
        "ELSE 0 END WHERE id IN (:w, :p, :f, :e)",
        {"w": work.ids[0], "p": parts.ids[0], "f": freight.ids[0], "e": eu_quote.line_ids.ids[0]},
    )
    assert q.amount_tax == 0  # Phase 1 totals live in the dropped column's world

    _script("post").migrate(env.elevated(), "1.0.0")

    assert not _has_column(env)
    company = env.ref("core.main_company")
    created = work.tax_ids
    assert created.amount == 19.6 and created.type_tax_use == "sale" and created.company_id == company
    tax_lines = created.repartition_line_ids.filtered(lambda r: r.repartition_type == "tax")
    assert set(tax_lines.mapped("account_id").ids) == set(chart_ref(company, "tax_due").ids)
    reused = parts.tax_ids
    assert reused.amount == 6 and reused.name == "State sales tax 6%"  # an existing tax of that rate
    assert not freight.tax_ids  # 0 % means no tax
    assert q.amount_untaxed == 260 and q.amount_tax == pytest.approx(39.2 + 3.0)
    assert q.amount_total == pytest.approx(302.2)
    assert q.invoice_status == "to_invoice" and not q.locked
    eu_line = eu_quote.line_ids
    assert eu_line.tax_ids.amount == 20 and eu_line.tax_ids.name == "VAT 20%" and eu_quote.amount_total == 120
    taxes = old_product.elevated().taxes_id
    assert sorted(taxes.mapped("amount")) == [7.0, 7.0] and len(taxes.mapped("company_id")) == 2
    # running it again changes nothing
    _script("post").migrate(env.elevated(), "1.0.0")
    assert work.tax_ids == created and q.amount_total == pytest.approx(302.2)

    _script("down").migrate(env.elevated(), "2.0.0")
    assert _has_column(env)
    rows = dict(
        env.cr.fetchall(
            "SELECT id, tax_percent FROM sale_quotation_line WHERE id IN (:w, :p, :f)",
            {"w": work.ids[0], "p": parts.ids[0], "f": freight.ids[0]},
        )
    )
    assert rows == {work.ids[0]: 19.6, parts.ids[0]: 6.0, freight.ids[0]: 0.0}
    env.cr.execute("ALTER TABLE sale_quotation_line DROP COLUMN tax_percent")


def test_fresh_install_converts_product_percentages(env: Any) -> None:
    """On install (demo data loaded before), products keep their Phase 1 rate as a tax and draft lines follow."""
    from modules.sale.hooks import post_init

    item = env["product.product"].create({"name": "Placeholder rate", "list_price": 10, "tax_percent": 12})
    item.write({"taxes_id": [("set", [])]})
    q = order(env, customer(env, "Fresh buyer"), [(item, 1)])
    assert not q.line_ids.tax_ids
    post_init(env.elevated())
    assert sorted(item.elevated().taxes_id.mapped("amount")) == [12.0, 12.0]
    assert q.line_ids.tax_ids.amount == 12 and q.amount_total == pytest.approx(11.2)
