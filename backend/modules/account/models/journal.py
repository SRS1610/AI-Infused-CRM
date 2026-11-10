"""Journals, fiscal years, lock dates and the company / partner accounting settings (ERD_PHASE_2 §3.1-3.2)."""

from __future__ import annotations

import datetime as dt
from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

JOURNAL_TYPES = [
    ("sale", "Sales"),
    ("purchase", "Purchase"),
    ("cash", "Cash"),
    ("bank", "Bank"),
    ("general", "Miscellaneous"),
]


def _company_default(self: Model) -> int | None:
    return self.env.company.ids[0] if self.env.company else None


class Journal(Model):
    _name = "account.journal"
    _description = "Journal"
    _order = "sequence, type, code"
    _audit = True
    _sql_constraints = [("code_company_uniq", "UNIQUE (code, company_id)", "Journal codes must be unique per company")]

    name = fields.Char(required=True, translate=True)
    code = fields.Char("Short code", required=True, size=5, help="Prefix of entry numbers (INV/2026/00001)")
    type = fields.Selection(JOURNAL_TYPES, required=True, index=True)
    sequence = fields.Integer(default=10)
    default_account_id = fields.Many2one(
        "account.account",
        "Default account",
        ondelete="restrict",
        help="Income account (sales), expense account (purchases), or the bank / cash account",
    )
    suspense_account_id = fields.Many2one(
        "account.account", "Suspense account", ondelete="restrict", help="Bank lines waiting to be reconciled"
    )
    currency_id = fields.Many2one("core.currency", "Journal currency", ondelete="restrict")
    bank_account_id = fields.Many2one("account.bank_account", "Bank account", ondelete="set_null")
    refund_sequence = fields.Boolean(
        "Separate credit note numbers", default=True, help="Credit notes are numbered R<code>/<year>/…"
    )
    restrict_mode_hash_table = fields.Boolean(
        "Lock posted entries with a hash", help="Posted entries are chained with SHA-256 (D-073)"
    )
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", default=_company_default)
    active = fields.Boolean(default=True)

    @api.depends("name", "code")
    def _compute_display_name(self) -> None:
        for j in self:
            j.display_name = f"{j.name} ({j.code})" if j.code else j.name

    @api.constrains("default_account_id", "company_id")
    def _check_company(self) -> None:
        for j in self:
            for acc in (j.default_account_id, j.suspense_account_id):
                if acc and acc.company_id.ids != j.company_id.ids:
                    raise ValidationError(_("The journal's accounts must belong to its company"))

    def write(self, vals: dict[str, Any]) -> bool:
        if {"code", "company_id", "type"} & set(vals) and self._has_posted_entries():
            raise UserError(_("The code, type and company of a journal with posted entries cannot change"))
        return super().write(vals)

    def _has_posted_entries(self) -> bool:
        return bool(
            self._ids
            and self.env.cr.scalar(
                "SELECT 1 FROM account_move WHERE journal_id = ANY(:ids) AND name IS NOT NULL AND name <> '/' LIMIT 1",
                {"ids": list(self._ids)},
            )
        )

    def unlink(self) -> bool:
        if self._ids and self.env.cr.scalar(
            "SELECT 1 FROM account_move WHERE journal_id = ANY(:ids) LIMIT 1", {"ids": list(self._ids)}
        ):
            raise UserError(_("A journal with entries cannot be deleted; archive it instead"))
        return super().unlink()

    def _sequence_for(self, year: int, refund: bool) -> Model:
        """The gapless numbering sequence of this journal for a fiscal year (created on first use, D-073)."""
        self.ensure_one()
        prefix_code = f"R{self.code}" if refund and self.refund_sequence else self.code
        code = f"account.journal.{self.ids[0]}.{'refund' if refund and self.refund_sequence else 'move'}.{year}"
        sequences = self.env["core.sequence"].elevated("journal numbering")
        seq = sequences.search([("code", "=", code), ("company_id", "=", self.company_id.ids[0])], limit=1)
        if not seq:
            seq = sequences.create(
                {
                    "name": f"{self.name} {year}{' (credit notes)' if refund else ''}",
                    "code": code,
                    "company_id": self.company_id.ids[0],
                    "prefix": f"{prefix_code}/{year}/",
                    "padding": 5,
                    "implementation": "gapless",
                }
            )
        return seq


class FiscalYear(Model):
    _name = "account.fiscal_year"
    _description = "Fiscal year"
    _order = "date_from desc"

    name = fields.Char(required=True)
    date_from = fields.Date("Start", required=True)
    date_to = fields.Date("End", required=True)
    state = fields.Selection([("open", "Open"), ("closed", "Closed")], default="open", required=True, readonly=True)
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", default=_company_default)

    @api.constrains("date_from", "date_to", "company_id")
    def _check_dates(self) -> None:
        for fy in self:
            if fy.date_to <= fy.date_from:
                raise ValidationError(_("A fiscal year ends after it starts"), field="date_to")
            overlap = self.elevated().search_count(
                [
                    ("id", "!=", fy.ids[0]),
                    ("company_id", "=", fy.company_id.ids[0]),
                    ("date_from", "<=", fy.date_to),
                    ("date_to", ">=", fy.date_from),
                ]
            )
            if overlap:
                raise ValidationError(_("Fiscal years of a company cannot overlap"), field="date_from")

    @api.model
    def year_of(self, company: Model, date: dt.date) -> tuple[int, dt.date, dt.date]:
        """(label year, start, end) of the fiscal year containing ``date`` (calendar year when none is defined).

        The label is the year the fiscal year ends in, so a July to June year 2026/27 numbers entries ``…/2027/…``.
        """
        fy = self.elevated().search(
            [("company_id", "=", company.ids[0]), ("date_from", "<=", date), ("date_to", ">=", date)], limit=1
        )
        if fy:
            return fy.date_to.year, fy.date_from, fy.date_to
        return date.year, dt.date(date.year, 1, 1), dt.date(date.year, 12, 31)

    @api.expose
    def action_close(self) -> bool:
        """Close the year: lock it for everyone. Reports carry its result to equity (no closing entry)."""
        for fy in self:
            if fy.state == "closed":
                continue
            company = fy.company_id.elevated()
            drafts = (
                self.env["account.move"]
                .elevated()
                .search_count(
                    [
                        ("company_id", "=", company.ids[0]),
                        ("state", "=", "draft"),
                        ("date", ">=", fy.date_from),
                        ("date", "<=", fy.date_to),
                    ]
                )
            )
            if drafts:
                raise UserError(
                    _("Post or delete the {count} draft entries of {name} first", count=drafts, name=fy.name)
                )
            if not company.fiscalyear_lock_date or company.fiscalyear_lock_date < fy.date_to:
                company.write({"fiscalyear_lock_date": fy.date_to})
            fy.elevated().write({"state": "closed"})
        return True


class CompanyAccounting(Model):
    _extend = "core.company"

    chart_template = fields.Char(readonly=True, help="Chart template installed for this company")
    account_fiscal_country_id = fields.Many2one("core.country", "Fiscal country", ondelete="set_null")
    default_receivable_account_id = fields.Many2one("account.account", ondelete="set_null")
    default_payable_account_id = fields.Many2one("account.account", ondelete="set_null")
    default_income_account_id = fields.Many2one("account.account", ondelete="set_null")
    default_expense_account_id = fields.Many2one("account.account", ondelete="set_null")
    account_suspense_id = fields.Many2one("account.account", "Bank suspense account", ondelete="set_null")
    account_outstanding_receipts_id = fields.Many2one("account.account", ondelete="set_null")
    account_outstanding_payments_id = fields.Many2one("account.account", ondelete="set_null")
    income_currency_exchange_account_id = fields.Many2one("account.account", "Exchange gain", ondelete="set_null")
    expense_currency_exchange_account_id = fields.Many2one("account.account", "Exchange loss", ondelete="set_null")
    account_epd_discount_expense_id = fields.Many2one("account.account", "Cash discount given", ondelete="set_null")
    account_epd_discount_income_id = fields.Many2one("account.account", "Cash discount received", ondelete="set_null")
    account_retained_earnings_id = fields.Many2one("account.account", "Retained earnings", ondelete="set_null")
    account_rounding_id = fields.Many2one("account.account", "Rounding differences", ondelete="set_null")
    exchange_journal_id = fields.Many2one("account.journal", "Exchange difference journal", ondelete="set_null")
    default_sale_tax_id = fields.Many2one("account.tax", "Default sales tax", ondelete="set_null")
    default_purchase_tax_id = fields.Many2one("account.tax", "Default purchase tax", ondelete="set_null")
    fiscalyear_lock_date = fields.Date("Lock date for everyone")
    period_lock_date = fields.Date("Lock date for non-advisers")
    tax_lock_date = fields.Date("Tax lock date")
    fiscalyear_last_day = fields.Integer(default=31)
    fiscalyear_last_month = fields.Integer(default=12)

    def write(self, vals: dict[str, Any]) -> bool:
        # companies are audited (core.audit_log), so every lock and unlock is recorded with its author
        lock_fields = {"fiscalyear_lock_date", "period_lock_date", "tax_lock_date"} & set(vals)
        if lock_fields and not self.env.is_system and not self.env.user.has_group("account.group_account_manager"):
            raise UserError(_("Only accounting advisers can change lock dates"))
        return super().write(vals)

    def _lock_date_for(self, *, tax: bool = False, adviser: bool | None = None) -> dt.date | None:
        """The last locked date for the current user (None when nothing is locked)."""
        self.ensure_one()
        me = self.elevated()
        if adviser is None:
            adviser = self.env.is_system or self.env.user.has_group("account.group_account_manager")
        dates = [me.fiscalyear_lock_date]
        if not adviser:
            dates.append(me.period_lock_date)
        if tax:
            dates.append(me.tax_lock_date)
        dates = [d for d in dates if d]
        return max(dates) if dates else None


class PartnerAccounting(Model):
    _extend = "contacts.partner"

    property_account_receivable_id = fields.Many2one(
        "account.account", "Receivable account", ondelete="set_null", domain=[["account_type", "=", "asset_receivable"]]
    )
    property_account_payable_id = fields.Many2one(
        "account.account", "Payable account", ondelete="set_null", domain=[["account_type", "=", "liability_payable"]]
    )
    property_payment_term_id = fields.Many2one("account.payment_term", "Customer payment terms", ondelete="set_null")
    property_supplier_payment_term_id = fields.Many2one(
        "account.payment_term", "Vendor payment terms", ondelete="set_null"
    )
    property_account_position_id = fields.Many2one("account.fiscal_position", "Fiscal position", ondelete="set_null")
    bank_ids = fields.One2many("account.bank_account", "partner_id", "Bank accounts")
    customer_rank = fields.Integer(default=0, copy=False)
    supplier_rank = fields.Integer(default=0, copy=False)

    def _accounting(self, company: Model, name: str) -> Model:
        """The partner's own value of ``name`` when it belongs to ``company``, else the company default (D-072)."""
        partner = self.elevated().commercial_partner_id or self.elevated()
        value = partner[name]
        if value and "company_id" in value._fields and value.company_id and value.company_id.ids != company.ids:
            value = value.browse([])
        if value:
            return value
        defaults = {
            "property_account_receivable_id": "default_receivable_account_id",
            "property_account_payable_id": "default_payable_account_id",
        }
        if name in defaults:
            return company.elevated()[defaults[name]]
        return value


class BankAccount(Model):
    _name = "account.bank_account"
    _description = "Bank account"
    _order = "sequence, id"
    _rec_name = "acc_number"
    _sql_constraints = [
        (
            "number_company_uniq",
            "UNIQUE (sanitized_acc_number, partner_id, company_id)",
            "This bank account is already registered for this partner",
        )
    ]

    partner_id = fields.Many2one("contacts.partner", "Holder", required=True, ondelete="cascade", index=True)
    acc_number = fields.Char("Account number (IBAN)", required=True)
    sanitized_acc_number = fields.Char(compute="_compute_sanitized", store=True, index=True)
    bank_name = fields.Char("Bank")
    bic = fields.Char("BIC / SWIFT")
    currency_id = fields.Many2one("core.currency", ondelete="set_null")
    sequence = fields.Integer(default=10)
    company_id = fields.Many2one("core.company", ondelete="cascade", help="Empty = shared by every company")
    active = fields.Boolean(default=True)

    @api.depends("acc_number")
    def _compute_sanitized(self) -> None:
        for bank in self:
            bank.sanitized_acc_number = "".join(ch for ch in (bank.acc_number or "") if ch.isalnum()).upper()

    @api.constrains("acc_number")
    def _check_iban(self) -> None:
        for bank in self:
            number = bank.sanitized_acc_number or ""
            if len(number) >= 15 and number[:2].isalpha() and number[2:4].isdigit() and not _iban_valid(number):
                raise ValidationError(_("{number} is not a valid IBAN", number=bank.acc_number), field="acc_number")


def _iban_valid(iban: str) -> bool:
    """ISO 13616 check digits (mod 97)."""
    moved = iban[4:] + iban[:4]
    digits = "".join(str(int(ch, 36)) for ch in moved)
    return int(digits) % 97 == 1
