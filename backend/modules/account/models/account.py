"""Chart of accounts: accounts and tags (D-073, ERD_PHASE_2 §3.1)."""

from __future__ import annotations

from tessera.exceptions import ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

ACCOUNT_TYPES = [
    ("asset_receivable", "Receivable"),
    ("asset_cash", "Bank and cash"),
    ("asset_current", "Current assets"),
    ("asset_prepayments", "Prepayments"),
    ("asset_non_current", "Non-current assets"),
    ("asset_fixed", "Fixed assets"),
    ("liability_payable", "Payable"),
    ("liability_credit_card", "Credit card"),
    ("liability_current", "Current liabilities"),
    ("liability_non_current", "Non-current liabilities"),
    ("equity", "Equity"),
    ("equity_unaffected", "Current year earnings"),
    ("income", "Income"),
    ("income_other", "Other income"),
    ("expense", "Expenses"),
    ("expense_depreciation", "Depreciation"),
    ("expense_direct_cost", "Cost of revenue"),
    ("off_balance", "Off-balance sheet"),
]
INTERNAL_GROUPS = [
    ("asset", "Asset"),
    ("liability", "Liability"),
    ("equity", "Equity"),
    ("income", "Income"),
    ("expense", "Expense"),
    ("off_balance", "Off balance"),
]
# account types whose balance is carried forward to the next fiscal year
BALANCE_SHEET_GROUPS = ("asset", "liability", "equity")


def internal_group(account_type: str | None) -> str | None:
    if not account_type:
        return None
    if account_type == "off_balance":
        return "off_balance"
    return account_type.split("_", 1)[0]


class AccountTag(Model):
    _name = "account.account_tag"
    _description = "Account tag"
    _order = "name"

    name = fields.Char(required=True, translate=True)
    applicability = fields.Selection([("accounts", "Accounts"), ("taxes", "Taxes")], default="accounts", required=True)
    country_id = fields.Many2one("core.country", ondelete="set_null")
    tax_negate = fields.Boolean(help="Tax-report grid tags: the amount is counted negatively")
    active = fields.Boolean(default=True)


class Account(Model):
    _name = "account.account"
    _description = "Account"
    _order = "code, company_id"
    _audit = True
    _global_search = ["code", "name"]
    _sql_constraints = [("code_company_uniq", "UNIQUE (code, company_id)", "Account codes must be unique per company")]

    code = fields.Char(required=True, size=64, index=True)
    name = fields.Char(required=True, translate=True, index="trigram")
    account_type = fields.Selection(ACCOUNT_TYPES, "Type", required=True, index=True)
    internal_group = fields.Selection(INTERNAL_GROUPS, compute="_compute_internal_group", store=True, index=True)
    reconcile = fields.Boolean("Allow reconciliation", help="Items can be matched (receivables, payables, suspense)")
    currency_id = fields.Many2one("core.currency", "Account currency", ondelete="restrict")
    company_id = fields.Many2one(
        "core.company",
        required=True,
        ondelete="cascade",
        default=lambda self: self.env.company.ids[0] if self.env.company else None,
    )
    tag_ids = fields.Many2many("account.account_tag", "Tags", domain=[["applicability", "=", "accounts"]])
    deprecated = fields.Boolean(help="Deprecated accounts cannot be used on new journal items")
    note = fields.Text("Internal notes")

    @api.depends("account_type")
    def _compute_internal_group(self) -> None:
        for acc in self:
            acc.internal_group = internal_group(acc.account_type)

    @api.depends("code", "name")
    def _compute_display_name(self) -> None:
        for acc in self:
            acc.display_name = f"{acc.code} {acc.name}" if acc.code else acc.name

    @api.constrains("account_type", "reconcile")
    def _check_reconcile(self) -> None:
        for acc in self:
            if acc.account_type in ("asset_receivable", "liability_payable") and not acc.reconcile:
                raise ValidationError(
                    _("Receivable and payable accounts must allow reconciliation ({code})", code=acc.code),
                    field="reconcile",
                )

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("account_type") in ("asset_receivable", "liability_payable"):
                vals["reconcile"] = True
        return super().create(batch)

    def write(self, vals):  # type: ignore[no-untyped-def]
        if "account_type" in vals or "company_id" in vals:
            used = self._used_ids()
            if used and ("company_id" in vals or self._type_change_breaks(vals.get("account_type"), used)):
                raise ValidationError(
                    _(
                        "This account has journal items: its company cannot change "
                        "and its type can only change within the same group"
                    )
                )
        if vals.get("account_type") in ("asset_receivable", "liability_payable"):
            vals = {**vals, "reconcile": True}
        return super().write(vals)

    def _used_ids(self) -> set[int]:
        if not self._ids:
            return set()
        rows = self.env.cr.execute(
            "SELECT DISTINCT account_id FROM account_move_line WHERE account_id = ANY(:ids)", {"ids": list(self._ids)}
        ).all()
        return {int(r[0]) for r in rows}

    def _type_change_breaks(self, new_type: str | None, used: set[int]) -> bool:
        if new_type is None:
            return False
        return any(internal_group(acc.account_type) != internal_group(new_type) for acc in self if acc.ids[0] in used)

    def unlink(self) -> bool:
        if self._used_ids():
            raise ValidationError(_("You cannot delete an account that has journal items; deprecate it instead"))
        return super().unlink()

    @api.model
    def by_code(self, code: str, company: Model | None = None) -> Model:
        company = company or self.env.company
        return self.search([("code", "=", code), ("company_id", "=", company.ids[0])], limit=1)
