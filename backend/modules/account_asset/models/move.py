"""Ledger extensions of the asset module: depreciation data on entries, assets from vendor bill lines,
and asset models on accounts."""

from __future__ import annotations

from tessera.exceptions import UserError
from tessera.i18n import _
from tessera.orm import Model, api, fields

ASSET_ACCOUNT_TYPES = ("asset_fixed", "asset_non_current")
ACCOUNTING_GROUPS = ("account.group_account_invoice", "account.group_account_readonly")


class AccountAssetSettings(Model):
    _extend = "account.account"

    create_asset = fields.Selection(
        [("no", "No"), ("draft", "Create in draft"), ("validate", "Create and confirm")],
        "Create assets",
        default="no",
        groups=ACCOUNTING_GROUPS,
        help="Posting a vendor bill line on this account creates an asset from the asset model",
    )
    asset_model_id = fields.Many2one(
        "account.asset",
        "Asset model",
        ondelete="set_null",
        domain=[["state", "=", "model"]],
        groups=ACCOUNTING_GROUPS,
    )


class MoveAsset(Model):
    _extend = "account.move"

    asset_id = fields.Many2one("account.asset", "Asset", ondelete="set_null", index=True, readonly=True, copy=False)
    asset_depreciation_value = fields.Monetary(
        "Depreciation", currency_field="company_currency_id", readonly=True, copy=False
    )
    asset_depreciation_units = fields.Float("Life units", digits=(16, 4), readonly=True, copy=False)
    asset_depreciated_value = fields.Monetary(
        "Cumulative depreciation", currency_field="company_currency_id", readonly=True, copy=False
    )
    asset_remaining_value = fields.Monetary(
        "Remaining to depreciate", currency_field="company_currency_id", readonly=True, copy=False
    )
    asset_value_change = fields.Boolean("Value change", readonly=True, copy=False)

    @api.expose
    def action_post(self) -> bool:
        result = super().action_post()
        bills = self.filtered(lambda m: m.state == "posted" and m.move_type == "in_invoice")
        if bills:
            bills._create_assets_from_models()
        return result

    def _asset_candidate_lines(self) -> Model:
        return self.mapped("line_ids").filtered(
            lambda line: line.display_type == "product" and line.account_id and not line.elevated().asset_ids
        )

    def _create_assets_from_models(self) -> None:
        assets = self.env["account.asset"]
        for line in self._asset_candidate_lines():
            account = line.account_id.elevated()
            if account.create_asset in ("draft", "validate") and account.asset_model_id:
                assets._create_from_lines(line, account.asset_model_id, validate=account.create_asset == "validate")

    @api.expose
    def action_create_assets(self) -> list[int]:
        """Create a draft asset for each line of these posted bills booked on a fixed-asset account."""
        created: list[int] = []
        for move in self:
            if move.state != "posted" or move.move_type != "in_invoice":
                raise UserError(_("Assets are created from posted vendor bills"))
            lines = move._asset_candidate_lines().filtered(
                lambda line: (
                    line.account_id.elevated().account_type in ASSET_ACCOUNT_TYPES
                    or line.account_id.elevated().asset_model_id
                )
            )
            if not lines:
                raise UserError(_("{name} has no line on a fixed-asset account without an asset", name=move.name))
            for line in lines:
                model = line.account_id.elevated().asset_model_id or None
                created += self.env["account.asset"]._create_from_lines(line, model).ids
        return created

    def _check_depreciation_change(self) -> None:
        for move in self:
            if move.asset_id and move.state == "posted" and not move.env.context.get("asset_internal"):
                raise UserError(
                    _(
                        "{name} is a posted depreciation entry of an asset: use Modify on the asset instead",
                        name=move.name,
                    )
                )

    @api.expose
    def button_draft(self) -> bool:
        self._check_depreciation_change()
        return super().button_draft()

    @api.expose
    def button_cancel(self) -> bool:
        self._check_depreciation_change()
        return super().button_cancel()


class MoveLineAsset(Model):
    _extend = "account.move_line"

    asset_ids = fields.Many2many(
        "account.asset",
        "Assets",
        relation="account_asset_move_line_rel",
        column1="line_id",
        column2="asset_id",
        copy=False,
        readonly=True,
    )

    @api.expose
    def action_create_asset(self, validate: bool = False) -> list[int]:
        """Create an asset from these posted purchase items (one asset per item)."""
        for line in self:
            if line.parent_state != "posted":
                raise UserError(_("Assets are created from posted journal items"))
            if line.elevated().asset_ids:
                raise UserError(_("Journal item '{label}' already has an asset", label=line.name or ""))
        out: list[int] = []
        for line in self:
            model = line.account_id.elevated().asset_model_id or None
            out += self.env["account.asset"]._create_from_lines(line, model, validate=bool(validate)).ids
        return out
