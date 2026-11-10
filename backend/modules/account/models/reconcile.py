"""Partial and full reconciliations (ERD_PHASE_2 §3.5). The matching logic is in ``modules.account.reconcile``."""

from __future__ import annotations

from tessera.orm import Model, fields


class PartialReconcile(Model):
    _name = "account.partial_reconcile"
    _description = "Partial reconciliation"
    _order = "id"

    debit_move_id = fields.Many2one("account.move_line", required=True, ondelete="cascade", index=True)
    credit_move_id = fields.Many2one("account.move_line", required=True, ondelete="cascade", index=True)
    amount = fields.Monetary(currency_field="company_currency_id", help="Matched amount in company currency")
    debit_amount_currency = fields.Monetary(currency_field="debit_currency_id")
    credit_amount_currency = fields.Monetary(currency_field="credit_currency_id")
    debit_currency_id = fields.Many2one("core.currency", related="debit_move_id.currency_id")
    credit_currency_id = fields.Many2one("core.currency", related="credit_move_id.currency_id")
    company_id = fields.Many2one("core.company", related="debit_move_id.company_id", store=True)
    company_currency_id = fields.Many2one("core.currency", related="debit_move_id.company_id.currency_id")
    full_reconcile_id = fields.Many2one("account.full_reconcile", ondelete="set_null", index=True)
    exchange_move_id = fields.Many2one("account.move", "Exchange difference entry", ondelete="set_null")
    max_date = fields.Date(help="Latest date of the two matched items")


class FullReconcile(Model):
    _name = "account.full_reconcile"
    _description = "Full reconciliation"
    _order = "id desc"

    name = fields.Char(required=True, default="/")
    partial_reconcile_ids = fields.One2many("account.partial_reconcile", "full_reconcile_id", "Partial matches")
    reconciled_line_ids = fields.One2many("account.move_line", "full_reconcile_id", "Matched items")
    exchange_move_id = fields.Many2one("account.move", "Exchange difference entry", ondelete="set_null")
