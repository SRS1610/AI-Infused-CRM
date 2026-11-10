"""Bank statements and statement lines (ERD_PHASE_2 §5).

Posting a statement line books ``bank account ↔ journal suspense account``; matching it (``matching.py``)
replaces the suspense item with the counterparts and reconciles them; undoing the match restores the suspense
item. Lines without an entry can still be edited; posted lines are read-only except for their notes.
"""

from __future__ import annotations

from typing import Any

from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.orm import Model, api, fields

LINE_STATES = [("draft", "Draft"), ("posted", "To reconcile"), ("reconciled", "Reconciled")]
LINE_EDITABLE_WHEN_POSTED = {"narration", "to_check"}  # the bank module itself writes with _bank_internal


class JournalBank(Model):
    _extend = "account.journal"

    bank_statement_ids = fields.One2many("account.bank_statement", "journal_id", "Statements")
    bank_csv_options = fields.Json(
        "CSV import options",
        help='Remembered CSV layout, e.g. {"columns": {"date": "Booking date", "amount": "Amount"}, '
        '"date_format": "%d/%m/%Y"}',
    )

    def _bank_suspense_account(self) -> Model:
        journal = self.elevated()
        account = journal.suspense_account_id or journal.company_id.account_suspense_id
        if not account:
            raise UserError(_("Journal {name} has no suspense account", name=journal.name))
        return account

    def _bank_currency(self) -> Model:
        journal = self.elevated()
        return journal.currency_id or journal.company_id.currency_id

    def _last_balance(self) -> float:
        last = (
            self.env["account.bank_statement"]
            .elevated()
            .search([("journal_id", "=", self.ids[0])], order="date desc, id desc", limit=1)
        )
        return float(last.balance_end_real or 0) if last else 0.0


class MoveBank(Model):
    _extend = "account.move"

    statement_line_id = fields.Many2one(
        "account.bank_statement_line", "Statement line", readonly=True, copy=False, ondelete="set_null", index=True
    )


class BankStatement(Model):
    _name = "account.bank_statement"
    _description = "Bank statement"
    _order = "date desc, id desc"
    _inherit = ["chatter.thread"]
    _audit = True

    name = fields.Char("Reference", required=True, default="/", tracking=True)
    journal_id = fields.Many2one(
        "account.journal",
        required=True,
        ondelete="restrict",
        index=True,
        domain=[["type", "in", ["bank", "cash"]]],
    )
    company_id = fields.Many2one("core.company", related="journal_id.company_id", store=True, index=True)
    currency_id = fields.Many2one("core.currency", compute="_compute_currency", store=True)
    date = fields.Date(required=True, default=lambda self: self.env.today(), index=True)
    balance_start = fields.Monetary("Opening balance")
    balance_end_real = fields.Monetary("Closing balance")
    balance_end = fields.Monetary("Computed closing balance", compute="_compute_balance_end", store=True)
    difference = fields.Monetary(compute="_compute_balance_end", store=True)
    state = fields.Selection(
        [("open", "Open"), ("posted", "Posted")], default="open", required=True, readonly=True, tracking=True
    )
    line_ids = fields.One2many("account.bank_statement_line", "statement_id", "Transactions", copy=True)
    import_file_id = fields.Many2one("core.attachment", "Imported file", ondelete="set_null", readonly=True)
    line_count = fields.Integer(compute="_compute_progress")
    reconciled_count = fields.Integer("Reconciled", compute="_compute_progress")

    @api.depends("journal_id")
    def _compute_currency(self) -> None:
        for st in self:
            st.currency_id = st.journal_id._bank_currency() if st.journal_id else None

    @api.depends("balance_start", "balance_end_real", "line_ids.amount")
    def _compute_balance_end(self) -> None:
        for st in self:
            total = sum(float(line.amount or 0) for line in st.line_ids)
            st.balance_end = float(st.balance_start or 0) + total
            st.difference = float(st.balance_end_real or 0) - st.balance_end

    @api.depends("line_ids.state")
    def _compute_progress(self) -> None:
        for st in self:
            st.line_count = len(st.line_ids)
            st.reconciled_count = len(st.line_ids.filtered(lambda line: line.state == "reconciled"))

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("journal_id") and "balance_start" not in vals:
                vals["balance_start"] = self.env["account.journal"].browse([int(vals["journal_id"])])._last_balance()
        statements = super().create(batch)
        for st in statements:
            if not st.name or st.name == "/":
                st.write({"name": f"{st.journal_id.elevated().code} {st.date.isoformat()}"})
        return statements

    def unlink(self) -> bool:
        if self.mapped("line_ids").filtered(lambda line: line.move_id):
            raise UserError(_("A statement with posted transactions cannot be deleted"))
        return super().unlink()

    @api.constrains("journal_id")
    def _check_journal(self) -> None:
        for st in self:
            if st.journal_id.elevated().type not in ("bank", "cash"):
                raise ValidationError(_("Statements belong to a bank or cash journal"), field="journal_id")

    @api.expose
    def action_post(self) -> bool:
        """Book every draft transaction (bank account ↔ suspense)."""
        for st in self:
            if st.currency_id and abs(st.currency_id.round(float(st.difference or 0))) > 0:
                raise UserError(
                    _(
                        "Statement {name}: the closing balance does not match the transactions (difference {diff})",
                        name=st.name,
                        diff=st.difference,
                    )
                )
            st.line_ids.filtered(lambda line: line.state == "draft")._post()
            st.write({"state": "posted"})
        return True

    @api.expose
    def action_auto_reconcile(self) -> dict[str, Any]:
        from modules.account_bank.rules import auto_reconcile

        return auto_reconcile(self.mapped("line_ids").filtered(lambda line: line.state == "posted"))


class BankStatementLine(Model):
    _name = "account.bank_statement_line"
    _description = "Bank transaction"
    _order = "date desc, sequence, id desc"
    _rec_name = "payment_ref"
    _sql_constraints = [
        (
            "unique_import_journal",
            "UNIQUE (journal_id, unique_import_id)",
            "This transaction has already been imported in this journal",
        )
    ]
    _indexes = [("journal_state", ["journal_id", "state"])]

    statement_id = fields.Many2one("account.bank_statement", "Statement", ondelete="cascade", index=True)
    journal_id = fields.Many2one("account.journal", required=True, ondelete="restrict", index=True)
    company_id = fields.Many2one("core.company", related="journal_id.company_id", store=True, index=True)
    currency_id = fields.Many2one("core.currency", compute="_compute_currency", store=True)
    sequence = fields.Integer(default=10)
    date = fields.Date(required=True, default=lambda self: self.env.today(), index=True)
    payment_ref = fields.Char("Label", index="trigram")
    ref = fields.Char("Bank reference", index=True)
    partner_id = fields.Many2one("contacts.partner", "Partner", ondelete="restrict", index=True)
    partner_name = fields.Char("Counterparty name")
    account_number = fields.Char("Counterparty account")
    amount = fields.Monetary(required=True)
    foreign_currency_id = fields.Many2one("core.currency", "Original currency", ondelete="restrict")
    amount_currency = fields.Monetary("Amount in original currency", currency_field="foreign_currency_id")
    transaction_type = fields.Char("Transaction type")
    unique_import_id = fields.Char(readonly=True, copy=False)
    narration = fields.Text("Details")
    move_id = fields.Many2one("account.move", "Journal entry", readonly=True, copy=False, ondelete="restrict")
    state = fields.Selection(LINE_STATES, default="draft", required=True, readonly=True, copy=False, index=True)
    is_reconciled = fields.Boolean(readonly=True, copy=False)
    amount_residual = fields.Monetary("Left to reconcile", readonly=True, copy=False)
    reconcile_model_id = fields.Many2one("account.reconcile_model", "Matched by rule", ondelete="set_null", copy=False)
    match_data = fields.Json(
        readonly=True, copy=False, help="Matches applied: {items: [{line_id, amount}], writeoffs: [...]}"
    )
    to_check = fields.Boolean("To check", copy=False)

    @api.depends("journal_id")
    def _compute_currency(self) -> None:
        for line in self:
            line.currency_id = line.journal_id._bank_currency() if line.journal_id else None

    @api.depends("payment_ref", "date", "amount")
    def _compute_display_name(self) -> None:
        for line in self:
            line.display_name = line.payment_ref or line.partner_name or (str(line.date) if line.date else "")

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        statements = self.env["account.bank_statement"]
        for vals in batch:
            if vals.get("statement_id") and not vals.get("journal_id"):
                vals["journal_id"] = statements.browse([int(vals["statement_id"])]).journal_id.ids[0]
            if not vals.get("partner_id") and (vals.get("account_number") or vals.get("partner_name")):
                partner = self._find_partner(vals.get("account_number"), vals.get("partner_name"))
                if partner:
                    vals["partner_id"] = partner.ids[0]
            vals.setdefault("amount_residual", vals.get("amount") or 0.0)
        return super().create(batch)

    def write(self, vals: dict[str, Any]) -> bool:
        if not self.env.context.get("_bank_internal"):
            for line in self:
                if line.move_id and set(vals) - LINE_EDITABLE_WHEN_POSTED:
                    raise UserError(
                        _(
                            "Transaction {name} is posted: undo its reconciliation and reset it first",
                            name=line.display_name,
                        )
                    )
        return super().write(vals)

    def unlink(self) -> bool:
        if self.filtered(lambda line: line.move_id):
            raise UserError(_("Posted transactions cannot be deleted"))
        return super().unlink()

    @api.model
    def _find_partner(self, account_number: str | None, name: str | None) -> Model | None:
        """The partner owning the counterparty account, else the only company partner with that exact name."""
        if account_number:
            clean = "".join(ch for ch in account_number if ch.isalnum()).upper()
            bank = self.env["account.bank_account"].elevated().search([("sanitized_acc_number", "=", clean)], limit=2)
            if len(bank) == 1:
                partner = bank.partner_id
                return partner.commercial_partner_id or partner
        if name and len(name.strip()) >= 3:
            wanted = " ".join(name.split()).lower()
            partners = (
                self.env["contacts.partner"]
                .elevated()
                .search([("name", "ilike", wanted), ("parent_id", "is_not_set")], limit=20)
                .filtered(lambda p: " ".join((p.name or "").split()).lower() == wanted)
            )
            if len(partners) == 1:
                return partners
        return None

    # -- posting -----------------------------------------------------------------------------------------

    def _liquidity_values(self) -> dict[str, Any]:
        journal = self.journal_id.elevated()
        company = journal.company_id
        ccur = company.currency_id
        jcur = journal._bank_currency()
        amount = float(self.amount or 0)
        balance = float(ccur.round(amount)) if jcur.ids == ccur.ids else jcur.convert(amount, ccur, company, self.date)
        if not journal.default_account_id:
            raise UserError(_("Journal {name} has no bank / cash account", name=journal.name))
        return {
            "name": self.payment_ref or self.ref or _("Bank transaction"),
            "account_id": journal.default_account_id.ids[0],
            "partner_id": self.partner_id.ids[0] if self.partner_id else None,
            "balance": balance,
            "amount_currency": amount,
            "currency_id": jcur.ids[0],
        }

    def _post(self) -> None:
        """Create and post the entry of each draft line: bank account ↔ suspense account."""
        for line in self:
            if line.move_id:
                continue
            if not line.amount:
                raise UserError(_("A bank transaction needs an amount"))
            journal = line.journal_id.elevated()
            liquidity = line._liquidity_values()
            suspense = {
                **liquidity,
                "account_id": journal._bank_suspense_account().ids[0],
                "balance": -liquidity["balance"],
                "amount_currency": -liquidity["amount_currency"],
            }
            move = (
                self.env["account.move"]
                .elevated("bank statement")
                .create(
                    {
                        "move_type": "entry",
                        "journal_id": journal.ids[0],
                        "company_id": journal.company_id.ids[0],
                        "currency_id": liquidity["currency_id"],
                        "date": line.date,
                        "ref": line.ref or line.payment_ref,
                        "partner_id": liquidity["partner_id"],
                        "statement_line_id": line.ids[0],
                        "line_ids": [("create", liquidity), ("create", suspense)],
                    }
                )
            )
            move.action_post()
            line.with_context(_bank_internal=True).write(
                {"move_id": move.ids[0], "state": "posted", "amount_residual": line.amount, "is_reconciled": False}
            )

    @api.expose
    def action_post(self) -> bool:
        self._post()
        return True

    # -- reconciliation (see modules.account_bank.matching / rules) ------------------------------------

    @api.expose
    def action_match(
        self, items: list[dict[str, Any]] | None = None, writeoffs: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        """Match this transaction with open items (``[{"line_id", "amount"?}]``) and write-offs
        (``[{"account_id", "balance"?, "label"?, "tax_id"?}]``); the rest stays on the suspense account."""
        from modules.account_bank.matching import match_line

        self.ensure_one()
        self.check_access("write")
        return match_line(self, items or [], writeoffs or [])

    @api.expose
    def action_unmatch(self) -> bool:
        from modules.account_bank.matching import unmatch_line

        self.check_access("write")
        for line in self:
            unmatch_line(line)
        return True

    @api.expose
    def get_suggestions(self, limit: int = 5) -> list[dict[str, Any]]:
        from modules.account_bank.rules import suggest_matches

        self.ensure_one()
        return [c.to_dict() for c in suggest_matches(self, limit=limit)]

    @api.expose
    def search_candidates(self, query: str = "", limit: int = 20) -> list[dict[str, Any]]:
        from modules.account_bank.rules import search_open_items

        self.ensure_one()
        return search_open_items(self, query, limit=limit)

    @api.expose
    def get_reconcile_info(self) -> dict[str, Any]:
        from modules.account_bank.matching import line_info

        self.ensure_one()
        return line_info(self)

    @api.model
    @api.expose
    def get_reconcile_lines(
        self, journal_id: int | None = None, show: str = "open", query: str = "", limit: int = 80
    ) -> list[dict[str, Any]]:
        """Transactions for the reconciliation screen (``show``: open / reconciled / all)."""
        from modules.account_bank.matching import line_info

        domain: list[Any] = [("state", "!=", "draft")]
        if journal_id:
            domain.append(("journal_id", "=", int(journal_id)))
        if show == "open":
            domain.append(("state", "=", "posted"))
        elif show == "reconciled":
            domain.append(("state", "=", "reconciled"))
        if query:
            domain.append(
                {"any": [("payment_ref", "ilike", query), ("partner_name", "ilike", query), ("ref", "ilike", query)]}
            )
        lines = self.search(domain, order="date desc, id desc", limit=min(int(limit), 500))
        return [line_info(line, brief=True) for line in lines]

    @api.model
    @api.expose
    def action_auto_reconcile_all(self, journal_id: int | None = None) -> dict[str, Any]:
        from modules.account_bank.rules import auto_reconcile

        domain: list[Any] = [("state", "=", "posted")]
        if journal_id:
            domain.append(("journal_id", "=", int(journal_id)))
        return auto_reconcile(self.search(domain))
