"""Links between invoices and sales order lines.

``account.move_line.sale_line_ids`` records which order lines an invoice line bills; the order lines' invoiced
quantities are computed from it. Credit notes made by reversing a linked invoice keep the links, and a
sales order is locked when an invoice that makes it fully invoiced is posted.
"""

from __future__ import annotations

from typing import Any

from modules.account.models.move import INTERNAL as ACCOUNT_INTERNAL
from modules.sale.models.quotation import INTERNAL
from tessera.orm import Model, api, fields


class MoveLineSale(Model):
    _extend = "account.move_line"

    sale_line_ids = fields.Many2many(
        "sale.quotation_line",
        "Sales order lines",
        relation="sale_order_line_invoice_rel",
        column1="invoice_line_id",
        column2="sale_line_id",
        copy=True,
        readonly=True,
    )

    def _touch_sale_lines(self, sale_line_ids: list[int]) -> None:
        if not sale_line_ids:
            return
        lines = self.env["sale.quotation_line"].elevated().browse(sorted(set(sale_line_ids)))
        self.env.cache.invalidate("sale.quotation_line", ["invoice_line_ids"], lines.ids)
        lines.modified(["invoice_line_ids"])
        self.env.flush()

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        records = super().create(vals_list)
        linked = [i for rec in records.elevated() for i in rec.sale_line_ids.ids]
        self._touch_sale_lines(linked)
        return records

    def write(self, vals: dict[str, Any]) -> bool:
        before = [i for rec in self.elevated() for i in rec.sale_line_ids.ids] if "sale_line_ids" in vals else []
        result = super().write(vals)
        if "sale_line_ids" in vals:
            self._touch_sale_lines(before + [i for rec in self.elevated() for i in rec.sale_line_ids.ids])
        return result

    def unlink(self) -> bool:
        linked = [i for rec in self.elevated() for i in rec.sale_line_ids.ids]
        result = super().unlink()
        self._touch_sale_lines(linked)
        return result


class MoveSale(Model):
    _extend = "account.move"

    sale_order_count = fields.Integer("Sales orders", compute="_compute_sale_orders")

    def _compute_sale_orders(self) -> None:
        for move in self:
            orders = move.elevated().line_ids.mapped("sale_line_ids").mapped("quotation_id")
            move.sale_order_count = len(orders)

    def _sale_orders(self) -> Model:
        return self.elevated().mapped("line_ids").mapped("sale_line_ids").mapped("quotation_id")

    @api.expose
    def action_post(self) -> bool:
        result = super().action_post()
        # a sales order is locked once an invoice makes it fully invoiced
        orders = self._sale_orders().filtered(
            lambda o: o.state == "accepted" and not o.locked and o.invoice_status == "invoiced"
        )
        if orders:
            orders.with_context(**{INTERNAL: True}).write({"locked": True})
        return result

    @api.expose
    def action_reverse(self, reason: str | None = None, date: str | None = None, mode: str = "refund") -> list[int]:
        ids = super().action_reverse(reason=reason, date=date, mode=mode)
        reversals = self.env["account.move"].browse(ids).elevated()
        for original in self.elevated():
            source = list(original.invoice_line_ids)
            if not any(line.sale_line_ids for line in source):
                continue
            for reversal in reversals.filtered(lambda m, o=original: m.reversed_entry_id.ids == o.ids):
                created = reversal.invoice_line_ids.sorted(lambda r: r.ids[0])
                for src, dst in zip(source, created, strict=False):
                    if src.sale_line_ids and not dst.sale_line_ids:
                        # link metadata only: the ledger content of a posted credit note does not change
                        dst.with_context(**{ACCOUNT_INTERNAL: True}).write(
                            {"sale_line_ids": [("set", src.sale_line_ids.ids)]}
                        )
        return ids
