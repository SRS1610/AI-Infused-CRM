"""Accounting settings on products, and payment registration from invoices."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.orm import Model, api, fields


class ProductAccounting(Model):
    _extend = "product.product"

    taxes_id = fields.Many2many(
        "account.tax",
        "Customer taxes",
        relation="product_taxes_rel",
        column1="product_id",
        column2="tax_id",
        domain=[["type_tax_use", "=", "sale"]],
        default=lambda self: self._default_taxes("sale"),
    )
    supplier_taxes_id = fields.Many2many(
        "account.tax",
        "Vendor taxes",
        relation="product_supplier_taxes_rel",
        column1="product_id",
        column2="tax_id",
        domain=[["type_tax_use", "=", "purchase"]],
        default=lambda self: self._default_taxes("purchase"),
    )
    property_account_income_id = fields.Many2one("account.account", "Income account", ondelete="set_null")
    property_account_expense_id = fields.Many2one("account.account", "Expense account", ondelete="set_null")
    purchase_ok = fields.Boolean("Can be purchased", default=True)

    def _variant_shared_fields(self) -> list[str]:
        return [
            *super()._variant_shared_fields(),
            "taxes_id",
            "supplier_taxes_id",
            "property_account_income_id",
            "property_account_expense_id",
            "purchase_ok",
        ]

    def _default_taxes(self, use: str) -> list[int]:
        company = self.env.company
        if not company:
            return []
        tax = company.elevated()["default_sale_tax_id" if use == "sale" else "default_purchase_tax_id"]
        return tax.ids if tax else []

    def _taxes_for(self, company: Model, use: str = "sale") -> Model:
        """The product's taxes that belong to ``company``."""
        taxes = self.elevated()["taxes_id" if use == "sale" else "supplier_taxes_id"]
        return self.env["account.tax"].browse(taxes.filtered(lambda t: t.company_id.ids == company.ids).ids)


class MovePayments(Model):
    _extend = "account.move"

    @api.expose
    def action_register_payment(
        self,
        journal_id: int | None = None,
        date: str | None = None,
        amount: float | None = None,
        currency_id: int | None = None,
        group: bool = True,
        writeoff_account_id: int | None = None,
        writeoff_label: str | None = None,
        communication: str | None = None,
    ) -> dict[str, Any]:
        """Pay these invoices (see ``modules.account.payments.register_payments``)."""
        from modules.account.payments import register_payments

        payments = register_payments(
            self,
            journal=self.env["account.journal"].browse([journal_id]) if journal_id else None,
            date=dt.date.fromisoformat(date) if date else None,
            amount=amount,
            currency=self.env["core.currency"].browse([currency_id]) if currency_id else None,
            group=group,
            writeoff_account=self.env["account.account"].browse([writeoff_account_id]) if writeoff_account_id else None,
            writeoff_label=writeoff_label,
            communication=communication,
        )
        return {"payment_ids": payments.ids, "names": payments.mapped("name")}
