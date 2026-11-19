"""Reconciliation rules (ERD_PHASE_2 §5): invoice matching and write-off suggestions.

The rule engine itself (candidate search, scoring, auto-reconciliation) is in ``modules.account_bank.rules``.
"""

from __future__ import annotations

import re
from typing import Any

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields


class ReconcileModel(Model):
    _name = "account.reconcile_model"
    _description = "Reconciliation rule"
    _order = "sequence, id"
    _audit = True

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    rule_type = fields.Selection(
        [("invoice_matching", "Match open invoices and payments"), ("writeoff_suggestion", "Suggest a write-off")],
        "Type",
        required=True,
        default="invoice_matching",
    )
    auto_reconcile = fields.Boolean(
        "Reconcile automatically", help="Apply the rule without review when it fully explains a transaction"
    )
    match_journal_ids = fields.Many2many(
        "account.journal",
        "Journals",
        relation="account_reconcile_model_journal_rel",
        column1="model_id",
        column2="journal_id",
        domain=[["type", "in", ["bank", "cash"]]],
        help="Empty = every bank and cash journal",
    )
    match_nature = fields.Selection(
        [("both", "Received and paid"), ("amount_received", "Money received"), ("amount_paid", "Money paid")],
        "Direction",
        default="both",
        required=True,
    )
    match_amount = fields.Selection(
        [("lower", "Is lower than"), ("greater", "Is greater than"), ("between", "Is between")], "Amount"
    )
    match_amount_min = fields.Float("Minimum amount", digits=(16, 2))
    match_amount_max = fields.Float("Maximum amount", digits=(16, 2))
    match_label = fields.Selection(
        [("contains", "Contains"), ("not_contains", "Does not contain"), ("match_regex", "Matches the expression")],
        "Label",
    )
    match_label_param = fields.Char("Label text or expression")
    match_partner = fields.Boolean("Partner is set")
    match_partner_ids = fields.Many2many(
        "contacts.partner",
        "Partners",
        relation="account_reconcile_model_partner_rel",
        column1="model_id",
        column2="partner_id",
    )
    match_text_location = fields.Boolean(
        "Find references in the label",
        default=True,
        help="Look for invoice numbers and payment references in the transaction's label and references",
    )
    match_same_amount = fields.Boolean(
        "Match the amount with the partner's open items",
        default=True,
        help="Without a reference, the partner and amount must match",
    )
    allow_payment_tolerance = fields.Boolean("Allow a payment difference")
    payment_tolerance_type = fields.Selection(
        [("percentage", "Percentage of the amount"), ("fixed_amount", "Fixed amount")],
        default="percentage",
        required=True,
    )
    payment_tolerance_param = fields.Float("Tolerance", digits=(16, 2), default=0.0)
    past_months_limit = fields.Integer("Search items of the last months", default=18)
    line_ids = fields.One2many("account.reconcile_model_line", "model_id", "Counterparts", copy=True)

    @api.constrains("match_label", "match_label_param")
    def _check_regex(self) -> None:
        for model in self:
            if model.match_label == "match_regex" and model.match_label_param:
                try:
                    re.compile(model.match_label_param)
                except re.error as exc:
                    raise ValidationError(
                        _("Invalid expression: {error}", error=str(exc)), field="match_label_param"
                    ) from exc

    @api.constrains("payment_tolerance_param")
    def _check_tolerance(self) -> None:
        for model in self:
            if (model.payment_tolerance_param or 0) < 0 or (
                model.payment_tolerance_type == "percentage" and (model.payment_tolerance_param or 0) > 100
            ):
                raise ValidationError(_("The tolerance must be between 0 and 100 %"), field="payment_tolerance_param")

    # -- conditions ----------------------------------------------------------------------------------

    def _matches_line(self, st_line: Model) -> list[str]:
        """Reason codes when the rule applies to the transaction; empty list when it does not."""
        self.ensure_one()
        amount = float(st_line.amount or 0)
        if self.match_journal_ids and st_line.journal_id.ids[0] not in self.match_journal_ids.ids:
            return []
        if self.match_nature == "amount_received" and amount < 0:
            return []
        if self.match_nature == "amount_paid" and amount > 0:
            return []
        absolute = abs(amount)
        low, high = float(self.match_amount_min or 0), float(self.match_amount_max or 0)
        if self.match_amount == "lower" and not absolute < high:
            return []
        if self.match_amount == "greater" and not absolute > low:
            return []
        if self.match_amount == "between" and not low <= absolute <= high:
            return []
        reasons = ["rule_conditions"]
        if self.match_label and self.match_label_param:
            text = " ".join(p for p in (st_line.payment_ref, st_line.ref, st_line.narration) if p)
            param = self.match_label_param
            if self.match_label == "contains" and param.lower() not in text.lower():
                return []
            if self.match_label == "not_contains" and param.lower() in text.lower():
                return []
            if self.match_label == "match_regex" and not re.search(param, text, flags=re.IGNORECASE):
                return []
            if self.match_label != "not_contains":
                reasons.append("label")
        if self.match_partner:
            if not st_line.partner_id:
                return []
            if self.match_partner_ids:
                commercial = st_line.partner_id.elevated().commercial_partner_id or st_line.partner_id
                if not set(commercial.ids) & set(self.match_partner_ids.ids):
                    return []
            reasons.append("partner_rule")
        return reasons

    def _tolerance(self, amount: float) -> float:
        if not self.allow_payment_tolerance:
            return 0.0
        param = float(self.payment_tolerance_param or 0)
        return abs(amount) * param / 100.0 if self.payment_tolerance_type == "percentage" else param

    def _writeoff_values(self, st_line: Model, residual: float) -> list[dict[str, Any]]:
        """Write-off specs (``balance`` signed like the counterpart) for a remaining amount of ``residual``."""
        out: list[dict[str, Any]] = []
        left = residual
        for line in self.line_ids.sorted(lambda r: (r.sequence or 0, r.ids[0])):
            if not left:
                break
            if line.amount_type == "fixed":
                value = min(abs(float(line.amount or 0)), abs(left)) * (1 if left > 0 else -1)
            elif line.amount_type == "regex" and line.amount_string:
                text = " ".join(p for p in (st_line.payment_ref, st_line.narration) if p)
                found = re.search(line.amount_string, text)
                if not found:
                    continue
                try:
                    value = float(found.group(1).replace(",", ".")) * (1 if left > 0 else -1)
                except (IndexError, ValueError):
                    continue
            else:
                value = left * float(line.amount or 100.0) / 100.0
            value = round(value, 2)
            out.append(
                {
                    "account_id": line.account_id.ids[0],
                    "balance": value,
                    "label": line.label or self.name,
                    "tax_id": line.tax_ids.ids[0] if line.tax_ids else None,
                }
            )
            left = round(left - value, 2)
        return out


class ReconcileModelLine(Model):
    _name = "account.reconcile_model_line"
    _description = "Reconciliation rule counterpart"
    _order = "sequence, id"

    model_id = fields.Many2one("account.reconcile_model", required=True, ondelete="cascade", index=True)
    sequence = fields.Integer(default=10)
    account_id = fields.Many2one("account.account", "Account", required=True, ondelete="cascade")
    label = fields.Char("Label")
    amount_type = fields.Selection(
        [("percentage", "Percentage of the balance"), ("fixed", "Fixed amount"), ("regex", "From the label")],
        "Amount type",
        default="percentage",
        required=True,
    )
    amount = fields.Float(digits=(16, 2), default=100.0)
    amount_string = fields.Char("Amount expression", help=r"Regular expression with one group, e.g. FEE (\d+[.,]\d+)")
    tax_ids = fields.Many2many(
        "account.tax", "Taxes", relation="account_reconcile_model_line_tax_rel", column1="line_id", column2="tax_id"
    )
    company_id = fields.Many2one("core.company", related="model_id.company_id", store=True)

    @api.constrains("account_id", "model_id")
    def _check_company(self) -> None:
        for line in self:
            if line.account_id.elevated().company_id.ids != line.model_id.company_id.ids:
                raise ValidationError(_("The account must belong to the rule's company"), field="account_id")
