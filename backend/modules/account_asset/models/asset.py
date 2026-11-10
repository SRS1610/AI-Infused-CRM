"""Fixed assets, asset models and depreciation boards (ERD_PHASE_2 §6, acceptance 2-g).

The schedule itself is computed by :mod:`modules.account_asset.board` (pure functions, documented there).
This model turns it into journal entries:

* **Board.** ``compute_depreciation_board`` deletes the draft depreciation entries and plans the remaining
  life as draft entries (expense debit / accumulated depreciation credit), dated at the end of each span. It
  starts after the last posted depreciation, from what is left of the depreciable value and of the life, so
  posted entries are never touched. Drafts are posted by the ``account_asset.post_depreciation`` job once
  due (an entry falling on or before the lock date is posted on the first open day instead).
* **Modify** (``action_modify``) sets a new book value on a date: depreciation up to that date is posted
  (prorated), then a decrease is posted as an extra depreciation (impairment) entry and the remaining
  life depreciates the lower value; an increase creates a *gross increase* child asset, posted against the
  revaluation counterpart account (the company's *Other income* account unless one is given), which
  depreciates over the parent's remaining life.
* **Pause / resume** posts the prorated depreciation up to the pause date and stops; resuming plans the
  remaining life from the resume date, so the end of life moves by the paused time.
* **Dispose / sell** post the prorated depreciation up to the date, then one closing entry for the asset and
  its gross increases: the gross value leaves the asset account, the accumulated depreciation is cleared,
  sale proceeds come from the invoice line's account (or the outstanding receipts account for a cash
  amount) and the difference with the book value is a gain or a loss (``asset_gain`` / ``asset_loss``).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from modules.account.chart import chart_ref
from modules.account.models.move import INTERNAL
from modules.account_asset import board
from tessera.exceptions import UserError, ValidationError
from tessera.i18n import _
from tessera.logging import get_logger
from tessera.orm import Model, api, fields

log = get_logger(__name__)

STATES = [
    ("model", "Model"),
    ("draft", "Draft"),
    ("open", "Running"),
    ("paused", "Paused"),
    ("close", "Closed"),
]
METHODS = [
    ("linear", "Straight line"),
    ("degressive", "Declining"),
    ("degressive_then_linear", "Declining then straight line"),
]
COMPUTATIONS = [
    ("none", "No prorata"),
    ("constant_periods", "Constant periods"),
    ("daily_computation", "Based on days per period"),
]
PERIODS = [("1", "Months"), ("12", "Years")]
SETTING_FIELDS = (
    "method",
    "method_number",
    "method_period",
    "method_progress_factor",
    "prorata_computation_type",
    "account_asset_id",
    "account_depreciation_id",
    "account_depreciation_expense_id",
    "journal_id",
    "asset_type",
)
BOARD_FIELDS = {
    "original_value",
    "salvage_value",
    "acquisition_date",
    "prorata_date",
    "prorata_computation_type",
    "method",
    "method_number",
    "method_period",
    "method_progress_factor",
}


def _company_default(self: Model) -> int | None:
    return self.env.company.ids[0] if self.env.company else None


def _as_date(value: Any, default: dt.date) -> dt.date:
    if not value:
        return default
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value)[:10])


class Asset(Model):
    _name = "account.asset"
    _description = "Fixed asset"
    _inherit = ["chatter.thread"]
    _order = "acquisition_date desc, id desc"
    _audit = True
    _global_search = ["name"]

    name = fields.Char(required=True, tracking=True, index="trigram")
    asset_type = fields.Selection(
        [("purchase", "Fixed asset"), ("expense", "Deferred expense")], "Type", required=True, default="purchase"
    )
    state = fields.Selection(
        STATES, default="draft", required=True, readonly=True, tracking=True, copy=False, index=True
    )
    model_id = fields.Many2one("account.asset", "Asset model", domain=[["state", "=", "model"]], ondelete="set_null")
    company_id = fields.Many2one("core.company", required=True, ondelete="cascade", default=_company_default)
    currency_id = fields.Many2one("core.currency", related="company_id.currency_id", string="Currency")
    original_value = fields.Monetary("Original value", currency_field="currency_id", default=0.0, tracking=True)
    salvage_value = fields.Monetary(
        "Salvage value", currency_field="currency_id", default=0.0, help="Value expected at the end of the life"
    )
    acquisition_date = fields.Date("Acquisition date", default=lambda self: self.env.today())
    prorata_date = fields.Date("Depreciation start", help="Defaults to the acquisition date")
    prorata_computation_type = fields.Selection(COMPUTATIONS, "Prorata", required=True, default="constant_periods")
    method = fields.Selection(METHODS, required=True, default="linear")
    method_number = fields.Integer("Number of depreciations", required=True, default=5)
    method_period = fields.Selection(PERIODS, "Depreciation period", required=True, default="12")
    method_progress_factor = fields.Float(
        "Declining factor", digits=(5, 4), default=0.3, help="Yearly rate of the declining methods (0.3 = 30 %)"
    )
    account_asset_id = fields.Many2one("account.account", "Asset account", ondelete="restrict")
    account_depreciation_id = fields.Many2one(
        "account.account", "Accumulated depreciation account", ondelete="restrict"
    )
    account_depreciation_expense_id = fields.Many2one(
        "account.account", "Depreciation expense account", ondelete="restrict"
    )
    journal_id = fields.Many2one("account.journal", "Journal", ondelete="restrict", domain=[["type", "=", "general"]])
    depreciation_move_ids = fields.One2many("account.move", "asset_id", "Depreciation board")
    original_move_line_ids = fields.Many2many(
        "account.move_line",
        "Purchase items",
        relation="account_asset_move_line_rel",
        column1="asset_id",
        column2="line_id",
        copy=False,
    )
    parent_id = fields.Many2one("account.asset", "Increase of", ondelete="cascade", readonly=True, index=True)
    children_ids = fields.One2many("account.asset", "parent_id", "Gross increases")
    life_units = fields.Float(
        "Useful life (units)", digits=(16, 4), readonly=True, copy=False, help="Periods, or days for daily prorata"
    )
    restart_date = fields.Date("Resumed on", readonly=True, copy=False)
    disposal_date = fields.Date("Disposal date", readonly=True, copy=False)
    disposal_move_id = fields.Many2one("account.move", "Disposal entry", readonly=True, copy=False, ondelete="set_null")
    book_value = fields.Monetary(
        compute="_compute_values", currency_field="currency_id", help="Original value - depreciation"
    )
    depreciated_value = fields.Monetary(
        "Depreciated", compute="_compute_values", currency_field="currency_id", help="Posted depreciation"
    )
    value_residual = fields.Monetary("Remaining to depreciate", compute="_compute_values", currency_field="currency_id")
    note = fields.Text("Notes")
    active = fields.Boolean(default=True)

    # -- computed ------------------------------------------------------------------------------------

    @api.depends(
        "original_value",
        "salvage_value",
        "depreciation_move_ids",
        "depreciation_move_ids.state",
        "depreciation_move_ids.asset_depreciation_value",
    )
    def _compute_values(self) -> None:
        for asset in self:
            done = sum(
                float(m.asset_depreciation_value or 0) for m in asset.depreciation_move_ids if m.state == "posted"
            )
            original = float(asset.original_value or 0)
            asset.depreciated_value = done
            asset.book_value = original - done
            asset.value_residual = original - float(asset.salvage_value or 0) - done

    @api.constrains("original_value", "salvage_value", "method_number", "method_progress_factor", "state")
    def _check_values(self) -> None:
        for asset in self:
            if asset.state == "model":
                continue
            if float(asset.original_value or 0) < 0 or float(asset.salvage_value or 0) < 0:
                raise ValidationError(_("Asset values cannot be negative"), field="original_value")
            if float(asset.salvage_value or 0) > float(asset.original_value or 0):
                raise ValidationError(_("The salvage value cannot exceed the original value"), field="salvage_value")
            if (asset.method_number or 0) < 1:
                raise ValidationError(_("An asset needs at least one depreciation"), field="method_number")
            if asset.method != "linear" and not 0 < float(asset.method_progress_factor or 0) <= 1:
                raise ValidationError(_("The declining factor must be between 0 and 1"), field="method_progress_factor")

    @api.constrains("account_asset_id", "account_depreciation_id", "account_depreciation_expense_id", "journal_id")
    def _check_accounts_company(self) -> None:
        for asset in self:
            company = asset.company_id.ids
            for rec in (
                asset.account_asset_id,
                asset.account_depreciation_id,
                asset.account_depreciation_expense_id,
                asset.journal_id,
            ):
                if rec and rec.elevated().company_id.ids != company:
                    raise ValidationError(_("The asset's accounts and journal must belong to its company"))

    # -- create / write ------------------------------------------------------------------------------

    def create(self, vals_list):  # type: ignore[no-untyped-def]
        batch = [dict(vals_list)] if isinstance(vals_list, dict) else [dict(v) for v in vals_list]
        for vals in batch:
            if vals.get("model_id"):
                model = self.elevated().browse([int(vals["model_id"])])
                for name in SETTING_FIELDS:
                    value = model[name]
                    if name in vals or value is None or value is False:
                        continue
                    if isinstance(value, Model):
                        if value:
                            vals[name] = value.ids[0]
                    else:
                        vals[name] = value
                vals.setdefault("company_id", model.company_id.ids[0])
            if not vals.get("prorata_date") and vals.get("acquisition_date") and vals.get("state") != "model":
                vals["prorata_date"] = vals["acquisition_date"]
            company = self.env["core.company"].browse([int(vals["company_id"])]) if vals.get("company_id") else None
            company = company or self.env.company
            if company and vals.get("state") != "model":
                for name, tid in (
                    ("account_depreciation_id", "accumulated_depreciation"),
                    ("account_depreciation_expense_id", "depreciation"),
                    ("journal_id", "journal_misc"),
                ):
                    if not vals.get(name):
                        rec = chart_ref(company, tid)
                        if rec is not None:
                            vals[name] = rec.ids[0]
                if not vals.get("account_asset_id"):
                    rec = chart_ref(company, "fixed_assets")
                    if rec is not None:
                        vals["account_asset_id"] = rec.ids[0]
        return super().create(batch)

    def write(self, vals: dict[str, Any]) -> bool:
        if BOARD_FIELDS & set(vals) and not self.env.context.get("asset_internal"):
            for asset in self:
                if asset.state in ("open", "paused", "close") and asset._posted_moves():
                    raise UserError(
                        _("{name} has posted depreciation: use Modify to change its value or life", name=asset.name)
                    )
        result = super().write(vals)
        if BOARD_FIELDS & set(vals) and not self.env.context.get("asset_internal"):
            for asset in self.filtered(lambda a: a.state in ("draft", "open")):
                if asset.state == "open" or asset.depreciation_move_ids:
                    asset.with_context(asset_internal=True).write({"life_units": asset._initial_life_units()})
                    asset.compute_depreciation_board()
        return result

    def unlink(self) -> bool:
        for asset in self:
            if asset.state not in ("draft", "model"):
                raise UserError(_("Only draft assets and models can be deleted ({name})", name=asset.name))
            drafts = asset.depreciation_move_ids.filtered(lambda m: m.state == "draft")
            if drafts:
                drafts.unlink()
        return super().unlink()

    # -- schedule helpers ----------------------------------------------------------------------------

    def _months(self) -> int:
        return int(self.method_period or "12")

    def _bounds(self) -> board.Bounds:
        if self._months() == 1:
            return board.monthly_bounds
        years = self.env["account.fiscal_year"]
        company = self.company_id

        def fiscal_bounds(day: dt.date) -> tuple[dt.date, dt.date]:
            _label, start, end = years.year_of(company, day)
            return start, end

        return fiscal_bounds

    def _round(self, amount: float) -> float:
        return float(self.company_id.elevated().currency_id.round(amount))

    def _prorata_start(self) -> dt.date:
        day = self.prorata_date or self.acquisition_date or self.env.today()
        if self.prorata_computation_type == "none":
            day = self._bounds()(day)[0]
        return day

    def _initial_life_units(self) -> float:
        return board.life_units(
            self.prorata_computation_type,
            self.prorata_date or self.acquisition_date or self.env.today(),
            int(self.method_number or 0),
            self._months(),
        )

    def _depreciation_moves(self) -> Model:
        return self.depreciation_move_ids.sorted(lambda m: (m.date, m.ids[0]))

    def _posted_moves(self) -> Model:
        return self._depreciation_moves().filtered(lambda m: m.state == "posted")

    def _start_date(self) -> dt.date:
        start = self._prorata_start()
        consumed = self._posted_moves().filtered(lambda m: float(m.asset_depreciation_units or 0) > 0)
        if consumed:
            start = max(start, max(consumed.mapped("date")) + dt.timedelta(days=1))
        if self.restart_date:
            start = max(start, self.restart_date)
        return start

    def _remaining_units(self) -> float:
        used = sum(float(m.asset_depreciation_units or 0) for m in self._posted_moves())
        return max(float(self.life_units or self._initial_life_units()) - used, 0.0)

    def _residual(self) -> float:
        posted = sum(float(m.asset_depreciation_value or 0) for m in self._posted_moves())
        return self._round(float(self.original_value or 0) - float(self.salvage_value or 0) - posted)

    def _schedule(self) -> list[board.Entry]:
        self.ensure_one()
        return board.schedule(
            method=self.method,
            residual=self._residual(),
            remaining_units=self._remaining_units(),
            start=self._start_date(),
            computation=self.prorata_computation_type,
            months=self._months(),
            factor=float(self.method_progress_factor or 0),
            bounds=self._bounds(),
            round_amount=self._round,
        )

    # -- entries -------------------------------------------------------------------------------------

    def _check_ready(self) -> None:
        for asset in self:
            missing = [
                label
                for label, rec in (
                    (_("asset account"), asset.account_asset_id),
                    (_("accumulated depreciation account"), asset.account_depreciation_id),
                    (_("depreciation expense account"), asset.account_depreciation_expense_id),
                    (_("journal"), asset.journal_id),
                )
                if not rec
            ]
            if missing:
                raise UserError(_("Set the {fields} of {name} first", fields=", ".join(missing), name=asset.name))
            if not asset.acquisition_date:
                raise UserError(_("Set the acquisition date of {name} first", name=asset.name))

    def _entry_vals(
        self, day: dt.date, amount: float, units: float, *, label: str | None = None, value_change: bool = False
    ) -> dict[str, Any]:
        label = label or str(_("{name}: depreciation", name=self.name))
        return {
            "move_type": "entry",
            "journal_id": self.journal_id.ids[0],
            "company_id": self.company_id.ids[0],
            "date": day,
            "ref": label,
            "asset_id": self.ids[0],
            "asset_depreciation_value": amount,
            "asset_depreciation_units": units,
            "asset_value_change": value_change,
            "line_ids": [
                (
                    "create",
                    {"name": label, "account_id": self.account_depreciation_expense_id.ids[0], "balance": amount},
                ),
                ("create", {"name": label, "account_id": self.account_depreciation_id.ids[0], "balance": -amount}),
            ],
        }

    def _moves(self) -> Model:
        return self.env["account.move"].elevated("asset depreciation entries")

    @api.expose
    def compute_depreciation_board(self) -> bool:
        """(Re)plan the draft depreciation entries of the remaining life."""
        for asset in self:
            if asset.state not in ("draft", "open"):
                raise UserError(_("The board of {name} cannot be computed in this state", name=asset.name))
            asset._check_ready()
            if not asset.life_units:
                asset.with_context(asset_internal=True).write({"life_units": asset._initial_life_units()})
            drafts = asset.depreciation_move_ids.filtered(lambda m: m.state == "draft")
            if drafts:
                drafts.unlink()
            vals = [asset._entry_vals(e.date, e.amount, e.units) for e in asset._schedule() if e.amount]
            if vals:
                self._moves().create(vals)
            asset._update_cumulative()
        return True

    def _update_cumulative(self) -> None:
        for asset in self:
            done = 0.0
            depreciable = float(asset.original_value or 0) - float(asset.salvage_value or 0)
            for move in asset._depreciation_moves():
                if move.state == "cancel":
                    continue
                done = asset._round(done + float(move.asset_depreciation_value or 0))
                vals = {"asset_depreciated_value": done, "asset_remaining_value": asset._round(depreciable - done)}
                if (
                    any(abs(float(move[k] or 0) - v) > 1e-9 for k, v in vals.items())
                    or move.asset_remaining_value is None
                ):
                    move.with_context(**{INTERNAL: True}).write(vals)

    def _post_entries(self, moves: Model) -> Model:
        """Post depreciation entries; one dated on or before the lock date moves to the first open day."""
        posted = self.env["account.move"]
        for move in moves.sorted(lambda m: (m.date, m.ids[0])):
            if move.state != "draft":
                continue
            lock = move.company_id.elevated()._lock_date_for(adviser=True)
            if lock and move.date <= lock:
                move.write({"date": lock + dt.timedelta(days=1)})
            move.action_post()
            posted |= move
        return posted

    @api.expose
    def action_post_due(self, date: str | None = None) -> int:
        """Post the draft depreciation entries dated up to ``date`` (default today) of running assets."""
        day = _as_date(date, self.env.today())
        count = 0
        for asset in self.filtered(lambda a: a.state == "open"):
            due = asset.depreciation_move_ids.filtered(lambda m: m.state == "draft" and m.date <= day)
            count += len(asset._post_entries(due))
            asset._update_cumulative()
        return count

    # -- lifecycle -----------------------------------------------------------------------------------

    @api.expose
    def action_validate(self) -> bool:
        for asset in self:
            if asset.state != "draft":
                raise UserError(_("Only draft assets can be confirmed ({name})", name=asset.name))
            asset._check_ready()
            if float(asset.original_value or 0) <= 0:
                raise UserError(_("Set the original value of {name} first", name=asset.name))
            vals: dict[str, Any] = {"state": "open"}
            if not asset.prorata_date:
                vals["prorata_date"] = asset.acquisition_date
            asset.with_context(asset_internal=True).write(vals)
            if not asset.life_units:
                asset.with_context(asset_internal=True).write({"life_units": asset._initial_life_units()})
            asset.compute_depreciation_board()
        return True

    @api.expose
    def action_set_to_draft(self) -> bool:
        for asset in self:
            if asset.state != "open" or asset._posted_moves():
                raise UserError(_("Only running assets without posted depreciation can go back to draft"))
            asset.depreciation_move_ids.filtered(lambda m: m.state == "draft").unlink()
            asset.with_context(asset_internal=True).write({"state": "draft"})
        return True

    def _cut(self, day: dt.date) -> Model:
        """Post the depreciation up to ``day`` (included): due entries, then a prorated one for the current span."""
        self.ensure_one()
        due = self.depreciation_move_ids.filtered(lambda m: m.state == "draft" and m.date <= day)
        self._post_entries(due)
        start = self._start_date()
        if start > day:
            return self.env["account.move"]
        entries = self._schedule()
        if not entries:
            return self.env["account.move"]
        first = entries[0]
        units, _fraction = board.span_units(self.prorata_computation_type, start, day, self._bounds(), self._months())
        units = min(units, first.units)
        amount = self._round(first.amount * units / first.units) if first.units else 0.0
        if not amount:
            return self.env["account.move"]
        move = self._moves().create(self._entry_vals(day, amount, units))
        self._post_entries(move)
        return move

    def _drop_drafts(self) -> None:
        drafts = self.mapped("depreciation_move_ids").filtered(lambda m: m.state == "draft")
        if drafts:
            drafts.unlink()

    @api.expose
    def action_pause(self, date: str | None = None) -> bool:
        day = _as_date(date, self.env.today())
        for asset in self:
            if asset.state != "open":
                raise UserError(_("Only running assets can be paused ({name})", name=asset.name))
            asset._check_after_last_entry(day)
            asset._cut(day)
            asset._drop_drafts()
            asset.with_context(asset_internal=True).write({"state": "paused"})
            asset._update_cumulative()
            asset.message_post(str(_("Depreciation paused on {date}", date=day)), internal=True)
        return True

    @api.expose
    def action_resume(self, date: str | None = None) -> bool:
        day = _as_date(date, self.env.today())
        for asset in self:
            if asset.state != "paused":
                raise UserError(_("Only paused assets can be resumed ({name})", name=asset.name))
            asset._check_after_last_entry(day - dt.timedelta(days=1))
            asset.with_context(asset_internal=True).write({"state": "open", "restart_date": day})
            asset.compute_depreciation_board()
            asset.message_post(str(_("Depreciation resumed on {date}", date=day)), internal=True)
        return True

    def _check_after_last_entry(self, day: dt.date) -> None:
        posted = self._posted_moves()
        if posted and day < max(posted.mapped("date")):
            raise UserError(
                _("{name} has depreciation posted after {date}", name=self.name, date=day),
                last=str(max(posted.mapped("date"))),
            )

    @api.expose
    def action_modify(
        self,
        value: float | str,
        date: str | None = None,
        reason: str | None = None,
        counterpart_account_id: int | None = None,
    ) -> list[int]:
        """Set a new book value on ``date``; returns the ids of the gross-increase assets created."""
        day = _as_date(date, self.env.today())
        new_value = float(value)
        created: list[int] = []
        for asset in self:
            if asset.state != "open":
                raise UserError(_("Only running assets can be modified ({name})", name=asset.name))
            if new_value < float(asset.salvage_value or 0):
                raise UserError(_("The new value of {name} cannot be lower than its salvage value", name=asset.name))
            asset._check_after_last_entry(day)
            asset._cut(day)
            book = asset._round(float(asset.original_value or 0) - asset._done())
            diff = asset._round(new_value - book)
            label = str(_("{name}: value change", name=asset.name)) + (f" ({reason})" if reason else "")
            if diff < 0:
                move = asset._moves().create(asset._entry_vals(day, -diff, 0.0, label=label, value_change=True))
                asset._post_entries(move)
            elif diff > 0:
                created.append(asset._gross_increase(day, diff, label, counterpart_account_id).ids[0])
            asset.compute_depreciation_board()
            asset.message_post(
                str(_("Book value changed from {old} to {new} on {date}", old=book, new=new_value, date=day))
                + (f": {reason}" if reason else ""),
                internal=True,
            )
        return created

    def _done(self) -> float:
        return sum(float(m.asset_depreciation_value or 0) for m in self._posted_moves())

    def _gross_increase(self, day: dt.date, amount: float, label: str, counterpart_id: int | None) -> Model:
        company = self.company_id
        counterpart = (
            self.env["account.account"].browse([int(counterpart_id)])
            if counterpart_id
            else chart_ref(company, "income_other")
        )
        if not counterpart:
            raise UserError(_("Choose the counterpart account of the value increase"))
        move = self._moves().create(
            {
                "move_type": "entry",
                "journal_id": self.journal_id.ids[0],
                "company_id": company.ids[0],
                "date": day,
                "ref": label,
                "line_ids": [
                    ("create", {"name": label, "account_id": self.account_asset_id.ids[0], "balance": amount}),
                    ("create", {"name": label, "account_id": counterpart.ids[0], "balance": -amount}),
                ],
            }
        )
        move.action_post()
        computation = self.prorata_computation_type
        child = self.elevated("gross increase").create(
            {
                "name": str(_("{name} (increase)", name=self.name)),
                "parent_id": self.ids[0],
                "company_id": company.ids[0],
                "asset_type": self.asset_type,
                "original_value": amount,
                "acquisition_date": day,
                "prorata_date": day + dt.timedelta(days=1),
                "prorata_computation_type": "constant_periods" if computation == "none" else computation,
                "method": self.method,
                "method_number": self.method_number,
                "method_period": self.method_period,
                "method_progress_factor": self.method_progress_factor,
                "account_asset_id": self.account_asset_id.ids[0],
                "account_depreciation_id": self.account_depreciation_id.ids[0],
                "account_depreciation_expense_id": self.account_depreciation_expense_id.ids[0],
                "journal_id": self.journal_id.ids[0],
                "original_move_line_ids": [("set", move.line_ids.filtered(lambda line: line.balance > 0).ids)],
            }
        )
        child.with_context(asset_internal=True).write({"life_units": self._remaining_units()})
        child.action_validate()
        return self.browse(child.ids)

    @api.expose
    def action_dispose(self, date: str | None = None, reason: str | None = None) -> int:
        """Scrap the asset: book value to *Loss on disposal of assets*. Returns the closing entry's id."""
        return self._close(_as_date(date, self.env.today()), 0.0, None, reason)

    @api.expose
    def action_sell(
        self, date: str | None = None, invoice_line_id: int | None = None, amount: float | str | None = None
    ) -> int:
        """Sell the asset for the amount of a customer invoice line, or for a cash ``amount``."""
        day = _as_date(date, self.env.today())
        if invoice_line_id:
            line = self.env["account.move_line"].browse([int(invoice_line_id)])
            if line.parent_state != "posted" or line.move_id.company_id.ids != self.company_id.ids:
                raise UserError(_("Choose a posted invoice line of the asset's company"))
            proceeds = -float(line.balance or 0)
            account = line.account_id
        elif amount not in (None, ""):
            proceeds = float(amount)  # type: ignore[arg-type]
            account = self.company_id.elevated().account_outstanding_receipts_id
            if not account:
                raise UserError(_("The company has no outstanding receipts account"))
        else:
            raise UserError(_("Give the sale invoice line or the amount received"))
        if proceeds < 0:
            raise UserError(_("The sale price cannot be negative"))
        return self._close(day, proceeds, account, None)

    def _close(self, day: dt.date, proceeds: float, proceeds_account: Model | None, reason: str | None) -> int:
        self.ensure_one()
        if self.state not in ("open", "paused") or self.parent_id:
            raise UserError(_("Only running or paused assets can be disposed of ({name})", name=self.name))
        family = self | self.children_ids.filtered(lambda a: a.state in ("open", "paused"))
        for asset in family:
            asset._check_after_last_entry(day)
            if asset.state == "open":
                asset._cut(day)
        family._drop_drafts()
        company = self.company_id
        gross: dict[int, float] = {}
        accumulated: dict[int, float] = {}
        for asset in family:
            gross[asset.account_asset_id.ids[0]] = gross.get(asset.account_asset_id.ids[0], 0.0) + float(
                asset.original_value or 0
            )
            accumulated[asset.account_depreciation_id.ids[0]] = (
                accumulated.get(asset.account_depreciation_id.ids[0], 0.0) + asset._done()
            )
        book = self._round(sum(gross.values()) - sum(accumulated.values()))
        difference = self._round(proceeds - book)
        label = (
            str(_("{name}: sale", name=self.name))
            if proceeds_account is not None
            else str(_("{name}: disposal", name=self.name))
        ) + (f" ({reason})" if reason else "")
        lines: list[tuple[str, dict[str, Any]]] = []
        for account_id, value in gross.items():
            lines.append(("create", {"name": label, "account_id": account_id, "balance": -self._round(value)}))
        for account_id, value in accumulated.items():
            if value:
                lines.append(("create", {"name": label, "account_id": account_id, "balance": self._round(value)}))
        if proceeds_account is not None and proceeds:
            lines.append(("create", {"name": label, "account_id": proceeds_account.ids[0], "balance": proceeds}))
        if difference:
            tid = "asset_gain" if difference > 0 else "asset_loss"
            account = chart_ref(company, tid)
            if account is None:
                raise UserError(_("The chart of accounts has no '{account}' account", account=tid))
            lines.append(("create", {"name": label, "account_id": account.ids[0], "balance": -difference}))
        move = self._moves().create(
            {
                "move_type": "entry",
                "journal_id": self.journal_id.ids[0],
                "company_id": company.ids[0],
                "date": day,
                "ref": label,
                "line_ids": lines,
            }
        )
        move.action_post()
        for asset in family:
            asset.with_context(asset_internal=True).write(
                {"state": "close", "disposal_date": day, "disposal_move_id": move.ids[0]}
            )
            asset._update_cumulative()
        self.message_post(
            str(_("Closed on {date}: book value {book}, proceeds {proceeds}", date=day, book=book, proceeds=proceeds)),
            internal=True,
        )
        return move.ids[0]

    # -- from vendor bills ---------------------------------------------------------------------------

    @api.model
    def _create_from_lines(self, lines: Model, model: Model | None = None, validate: bool = False) -> Model:
        """One asset per journal item (vendor bill line), from ``model`` or the company defaults."""
        created = self.browse([])
        for line in lines:
            move = line.move_id
            vals: dict[str, Any] = {
                "name": line.name or (line.product_id.name if line.product_id else move.name),
                "company_id": move.company_id.ids[0],
                "original_value": abs(float(line.balance or 0)),
                "acquisition_date": move.invoice_date or move.date,
                "account_asset_id": line.account_id.ids[0],
                "original_move_line_ids": [("set", line.ids)],
            }
            if model:
                vals["model_id"] = model.ids[0]
            asset = self.elevated("asset from vendor bill").create(vals)
            if validate:
                asset.action_validate()
            created |= self.browse(asset.ids)
        return created
