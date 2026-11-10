"""Tax engine golden set (2-e, 40 cases), fiscal positions and payment terms."""

from __future__ import annotations

from typing import Any

import pytest

from modules.account.tests.common import D, eu_env, invoice, partner

pytestmark = pytest.mark.db


def T(amount: float, kind: str = "percent", **kw: Any) -> dict[str, Any]:  # noqa: N802
    return {"amount": amount, "amount_type": kind, **kw}


INCL = {"price_include": True}
BASE = {"include_base_amount": True}
NOAFF = {"is_base_affected": False}

# (taxes, price_unit, quantity, total excluded, total included, tax amounts in engine order)
GOLDEN: list[tuple[list[dict[str, Any]], float, float, float, float, list[float]]] = [
    ([T(10)], 100, 1, 100, 110, [10]),
    ([T(10)], 33.33, 3, 99.99, 109.99, [10.0]),
    ([T(20, **INCL)], 120, 1, 100, 120, [20]),
    ([T(20, **INCL)], 100, 1, 83.33, 100, [16.67]),
    ([T(10, **INCL), T(11, **INCL, sequence=2)], 121, 1, 100, 121, [10, 11]),
    ([T(10), T(5, sequence=2)], 200, 1, 200, 230, [20, 10]),
    ([T(10, **BASE), T(5, sequence=2)], 100, 1, 100, 115.5, [10, 5.5]),
    ([T(10, **BASE), T(5, sequence=2, **NOAFF)], 100, 1, 100, 115, [10, 5]),
    ([T(2.5, "fixed")], 10, 4, 40, 50, [10]),
    ([T(1, "fixed", **INCL)], 11, 1, 10, 11, [1]),
    ([T(10, "division")], 90, 1, 90, 100, [10]),
    ([T(10, "division", **INCL)], 100, 1, 90, 100, [10]),
    ([T(0, "group", children=[T(6), T(2.5, sequence=2)])], 100, 1, 100, 108.5, [6, 2.5]),
    ([T(0)], 100, 1, 100, 100, [0]),
    ([T(10)], -50, 1, -50, -55, [-5]),
    ([T(20, **INCL)], 59.99, 1, 49.99, 59.99, [10.0]),
    ([T(5.5)], 19.99, 7, 139.93, 147.63, [7.70]),
    ([T(8.875)], 1234.56, 1, 1234.56, 1344.13, [109.57]),
    ([T(21, **INCL)], 1, 1, 0.83, 1, [0.17]),
    ([T(21)], 0.01, 1, 0.01, 0.01, [0.0]),
    ([T(10), T(1, "fixed", sequence=2)], 10, 2, 20, 24, [2, 2]),
    ([T(1, "fixed", **BASE), T(10, sequence=2)], 10, 2, 20, 24.2, [2, 2.2]),
    ([T(20, **INCL), T(10, sequence=2)], 120, 1, 100, 130, [20, 10]),
    ([T(15)], 99.98, 0.5, 49.99, 57.49, [7.50]),
    ([T(10), T(10, sequence=2, **INCL)], 110, 1, 100, 120, [10, 10]),
    ([T(10, **INCL, **BASE), T(10, sequence=2, **INCL)], 121, 1, 100, 121, [10, 11]),
    ([T(0.5, "fixed"), T(20, sequence=2, **NOAFF)], 10, 3, 30, 37.5, [1.5, 6]),
    ([T(7)], 0.123, 1000, 123, 131.61, [8.61]),
    ([T(19)], 84.03, 1, 84.03, 100.0, [15.97]),
    ([T(19, **INCL)], 100, 1, 84.03, 100, [15.97]),
    ([T(25)], 80, 1.5, 120, 150, [30]),
    ([T(12.5, **INCL)], 45, 1, 40, 45, [5]),
    ([T(0, "group", children=[T(5), T(9.975, sequence=2)])], 100, 1, 100, 114.98, [5, 9.98]),
    ([T(5, "division")], 95, 1, 95, 100, [5]),
    ([T(3, "fixed")], 10, -2, -20, -26, [-6]),
    ([T(10)], 100, 0, 0, 0, [0]),
    ([T(20, **INCL)], 12, 3, 30, 36, [6]),
    ([T(20, reverse_charge=True)], 100, 1, 100, 100, [20, -20]),
    ([T(1), T(2, sequence=2), T(3, sequence=3)], 1000, 1, 1000, 1060, [10, 20, 30]),
    ([T(20, **INCL)], 9.99, 7, 58.27, 69.93, [11.66]),
]


def _make(env: Any, spec: dict[str, Any], n: int, i: int) -> Any:
    tax_account = env["account.account"].search([("code", "=", "2110"), ("company_id", "=", env.company.ids[0])])
    deductible = env["account.account"].search([("code", "=", "1410"), ("company_id", "=", env.company.ids[0])])
    vals: dict[str, Any] = {
        "name": f"Golden {n}.{i}",
        "amount": spec["amount"],
        "amount_type": spec["amount_type"],
        "price_include": spec.get("price_include", False),
        "include_base_amount": spec.get("include_base_amount", False),
        "is_base_affected": spec.get("is_base_affected", True),
        "sequence": spec.get("sequence", 1),
        "type_tax_use": "sale",
    }
    if spec["amount_type"] == "group":
        vals["children_tax_ids"] = [
            ("set", [_make(env, child, n, 10 + j).ids[0] for j, child in enumerate(spec["children"])])
        ]
        vals["type_tax_use"] = "sale"
    elif spec.get("reverse_charge"):
        vals["repartition_line_ids"] = [
            ("create", {"document_type": doc, "repartition_type": kind, "factor_percent": factor, "account_id": acc})
            for doc in ("invoice", "refund")
            for kind, factor, acc in (
                ("base", 100.0, None),
                ("tax", 100.0, deductible.ids[0]),
                ("tax", -100.0, tax_account.ids[0]),
            )
        ]
    else:
        vals["repartition_line_ids"] = [
            ("create", {"document_type": doc, "repartition_type": kind, "factor_percent": 100.0, "account_id": acc})
            for doc in ("invoice", "refund")
            for kind, acc in (("base", None), ("tax", tax_account.ids[0]))
        ]
    if spec["amount_type"] == "group":
        vals.pop("repartition_line_ids", None)
    return env["account.tax"].create(vals)


@pytest.mark.parametrize("n", range(len(GOLDEN)))
def test_golden_tax_case(env, n):
    specs, price, qty, excl, incl, amounts = GOLDEN[n]
    taxes = env["account.tax"].browse([_make(env, spec, n, i).ids[0] for i, spec in enumerate(specs)])
    currency = env.company.currency_id
    res = taxes.compute_all(price, currency, qty)
    assert res["total_excluded"] == pytest.approx(excl), res
    assert res["total_included"] == pytest.approx(incl), res
    assert [t["amount"] for t in res["taxes"]] == pytest.approx(amounts), res
    # the same line on an invoice produces the matching entries
    customer = partner(env, f"Golden customer {n}")
    move = invoice(
        env,
        customer,
        [{"name": "Golden", "price_unit": price, "quantity": qty, "tax_ids": [("set", taxes.ids)]}],
        post=False,
    )
    assert move.amount_untaxed == pytest.approx(excl)
    assert move.amount_tax == pytest.approx(round(sum(amounts), 2))
    assert sum(float(line.balance) for line in move.line_ids) == pytest.approx(0, abs=1e-9)
    term = move.line_ids.filtered(lambda r: r.display_type == "payment_term")
    assert sum(float(line.amount_currency) for line in term) == pytest.approx(incl)
    if move.amount_total >= 0 and qty:
        move.action_post()
        assert move.state == "posted"


def test_refund_uses_refund_repartition(env):
    tax = env["account.tax"].search(
        [
            ("company_id", "=", env.company.ids[0]),
            ("type_tax_use", "=", "sale"),
            ("amount_type", "=", "percent"),
            ("amount", ">", 0),
        ],
        limit=1,
    )
    inv = invoice(env, partner(env), [{"price_unit": 100.0, "tax_ids": [("set", tax.ids)]}], move_type="out_refund")
    tax_line = inv.line_ids.filtered(lambda r: r.display_type == "tax")
    assert tax_line.tax_repartition_line_id.document_type == "refund"
    assert all(t.name.startswith("-") for t in tax_line.tax_tag_ids)
    assert float(tax_line.balance) > 0  # a customer refund debits the tax account


def test_us_group_tax_from_chart(env):
    combined = env.ref(f"account.{env.company.ids[0]}_sale_combined_8_5")
    move = invoice(env, partner(env), [{"price_unit": 200.0, "tax_ids": [("set", combined.ids)]}])
    assert move.amount_tax == pytest.approx(17.0)
    assert sorted(float(x.balance) for x in move.line_ids.filtered(lambda r: r.display_type == "tax")) == [-12.0, -5.0]


def test_eu_fiscal_positions(env):
    e = eu_env(env)
    company = e.company
    vat20 = e.ref(f"account.{company.ids[0]}_sale_vat_20")
    countries = e["core.country"]
    de = countries.search([("code", "=", "DE")])
    us = countries.search([("code", "=", "US")])
    fr = countries.search([("code", "=", "FR")])
    b2b = partner(e, "Berlin GmbH", country_id=de.ids[0], vat="DE123456789")
    b2c = partner(e, "Berlin consumer", country_id=de.ids[0])
    export = partner(e, "New York Inc", country_id=us.ids[0])
    domestic = partner(e, "Paris SARL", country_id=fr.ids[0], vat="FR12345678901")
    fp = e["account.fiscal_position"]
    assert fp._get_for_partner(b2b, company) == e.ref(f"account.{company.ids[0]}_fp_eu_b2b")
    assert not fp._get_for_partner(b2c, company)
    assert fp._get_for_partner(export, company) == e.ref(f"account.{company.ids[0]}_fp_export")
    assert not fp._get_for_partner(domestic, company)
    move = invoice(e, b2b, [{"price_unit": 1000.0, "tax_ids": [("set", vat20.ids)]}])
    assert move.fiscal_position_id
    assert move.amount_tax == 0
    assert move.invoice_line_ids.tax_ids == e.ref(f"account.{company.ids[0]}_sale_vat_0_eu")
    assert move.name.startswith("INV/2026/")


def test_reverse_charge_on_eu_purchase(env):
    e = eu_env(env)
    company = e.company
    de = e["core.country"].search([("code", "=", "DE")])
    vendor = partner(e, "Munich Software GmbH", country_id=de.ids[0], vat="DE999999999")
    vat20 = e.ref(f"account.{company.ids[0]}_purchase_vat_20")
    bill = invoice(e, vendor, [{"price_unit": 500.0, "tax_ids": [("set", vat20.ids)]}], move_type="in_invoice")
    taxes = bill.line_ids.filtered(lambda r: r.display_type == "tax")
    assert sorted(float(x.balance) for x in taxes) == [-100.0, 100.0]  # due and deductible cancel out
    assert bill.amount_total == pytest.approx(500.0)
    grids = sorted(t.name for x in taxes for t in x.tax_tag_ids)
    assert grids == ["+08", "+09"]


def test_payment_terms_installments(env):
    term = env.ref("account.term_30_now_balance_60")
    move = invoice(
        env, partner(env), [{"price_unit": 1000.0}], payment_term_id=term.ids[0], invoice_date=D(2026, 3, 10)
    )
    term_lines = move.line_ids.filtered(lambda r: r.display_type == "payment_term").sorted(lambda r: r.date_maturity)
    assert [(str(x.date_maturity), float(x.balance)) for x in term_lines] == [
        ("2026-03-10", 300.0),
        ("2026-05-09", 700.0),
    ]
    assert move.invoice_date_due == D(2026, 5, 9)


def test_payment_term_end_of_month_and_preview(env):
    term = env.ref("account.term_end_following_month")
    assert term.preview("2026-01-15", 100.0) == [{"date": "2026-02-28", "amount": 100.0}]
    custom = env["account.payment_term"].create(
        {
            "name": "Fixed 100 then balance",
            "line_ids": [
                ("create", {"value": "fixed", "value_amount": 100, "nb_days": 0, "sequence": 1}),
                ("create", {"value": "balance", "nb_days": 30, "delay_type": "days_after_end_of_month", "sequence": 2}),
            ],
        }
    )
    assert custom.preview("2026-01-15", 250.0) == [
        {"date": "2026-01-15", "amount": 100.0},
        {"date": "2026-03-02", "amount": 150.0},
    ]


def test_payment_term_needs_one_balance_line_last(env):
    from tessera.exceptions import ValidationError

    with pytest.raises(ValidationError):
        env["account.payment_term"].create(
            {"name": "Bad", "line_ids": [("create", {"value": "percent", "value_amount": 50})]}
        )
